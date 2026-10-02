"""HTTP entry point for TestLattice."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .service import CASE_KINDS, Service, ServiceError


def env_address() -> tuple[str, int]:
    raw = os.environ.get("TESTLATTICE_ADDR", "127.0.0.1:8080")
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid TESTLATTICE_ADDR: {raw!r}")
    return host, int(port)


def _parse_case_filters(query: str) -> dict:
    params = parse_qs(query, keep_blank_values=True)
    filters: dict = {}
    suite_id = None
    if "suite_id" in params:
        suite_id = params["suite_id"][0].strip()
        if not suite_id:
            raise ServiceError(400, "validation_error", "suite_id filter must not be empty")
        filters["suite_id"] = suite_id
    if "include_descendants" in params:
        if suite_id is None:
            raise ServiceError(
                400, "validation_error", "include_descendants requires suite_id"
            )
        raw = params["include_descendants"][0]
        if raw not in ("true", "false"):
            raise ServiceError(
                400, "validation_error", "include_descendants must be true or false"
            )
        filters["include_descendants"] = raw == "true"
    if "kind" in params:
        kind = params["kind"][0]
        if kind not in CASE_KINDS:
            raise ServiceError(
                400, "validation_error", f"kind must be one of: {', '.join(sorted(CASE_KINDS))}"
            )
        filters["kind"] = kind
    if "enabled" in params:
        raw = params["enabled"][0]
        if raw not in ("true", "false"):
            raise ServiceError(400, "validation_error", "enabled must be true or false")
        filters["enabled"] = raw == "true"
    if "tags" in params:
        tags = [tag.strip() for tag in params["tags"][0].split(",")]
        if not tags or any(not tag for tag in tags):
            raise ServiceError(400, "validation_error", "tags filter must be non-empty")
        filters["tags"] = tags
    return filters


class Handler(BaseHTTPRequestHandler):
    service = Service()

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self) -> object:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServiceError(400, "invalid_json", "request body is not valid JSON") from None

    def _not_found(self) -> None:
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def _route(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        try:
            if method == "GET" and path == "/healthz":
                self.send_json(200, self.service.health())
                return
            if path == "/v1/suites":
                if method == "POST":
                    self.send_json(201, self.service.create_suite(self._read_json()))
                    return
                if method == "GET":
                    self.send_json(200, self.service.list_suites())
                    return
            elif path.startswith("/v1/suites/"):
                suite_id = unquote(path[len("/v1/suites/"):])
                if suite_id:
                    if method == "GET":
                        self.send_json(200, self.service.get_suite(suite_id))
                        return
                    if method == "DELETE":
                        self.service.delete_suite(suite_id)
                        self.send_empty(204)
                        return
            elif path == "/v1/cases":
                if method == "POST":
                    self.send_json(201, self.service.create_case(self._read_json()))
                    return
                if method == "GET":
                    filters = _parse_case_filters(parsed.query)
                    self.send_json(200, self.service.list_cases(**filters))
                    return
            elif path.startswith("/v1/cases/"):
                case_id = unquote(path[len("/v1/cases/"):])
                if case_id:
                    if method == "GET":
                        self.send_json(200, self.service.get_case(case_id))
                        return
                    if method == "DELETE":
                        self.service.delete_case(case_id)
                        self.send_empty(204)
                        return
            self._not_found()
        except ServiceError as exc:
            self.send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence per-request logging so recorded output stays stable."""


def main() -> int:
    parser = argparse.ArgumentParser(prog="testlattice.server", description="测试、QA 与端到端编排判定平台")
    host, port = env_address()
    parser.add_argument("--host", default=host)
    parser.add_argument("--port", type=int, default=port)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"TestLattice listening on http://{args.host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
