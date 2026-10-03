"""HTTP entry point for TestLattice."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .assertions import evaluate_assertions
from .junit import render_junit_xml
from .service import ALLOWED_KINDS, ApiError, Service
from .snapshots import validate_compare_request


def env_address() -> tuple[str, int]:
    raw = os.environ.get("TESTLATTICE_ADDR", "127.0.0.1:8080")
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid TESTLATTICE_ADDR: {raw!r}")
    return host, int(port)


def _first(values: dict[str, list[str]], key: str) -> object:
    return values[key][0] if key in values else None


def _resource_id(path: str, prefix: str) -> str | None:
    if not path.startswith(prefix):
        return None
    tail = unquote(path[len(prefix):])
    if not tail or "/" in tail:
        return None
    return tail


def _subresource(path: str, prefix: str, suffix: str) -> str | None:
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    middle = unquote(path[len(prefix):-len(suffix)])
    if not middle or "/" in middle:
        return None
    return middle


class Handler(BaseHTTPRequestHandler):
    service = Service()

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_no_content(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_error_body(self, status: int, code: str, message: str) -> None:
        self.send_json(status, {"error": {"code": code, "message": message}})

    def send_api_error(self, error: ApiError) -> None:
        self.send_error_body(error.status, error.code, error.message)

    def send_xml(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def read_body(self) -> object:
        length_raw = self.headers.get("Content-Length")
        try:
            length = int(length_raw) if length_raw is not None else 0
        except ValueError:
            raise ApiError(400, "invalid_json", "request body is not valid JSON")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "invalid_json", "request body is not valid JSON")

    # -- GET ------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, self.service.health())
            return
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/v1/suites":
            self.handle_list_suites()
            return
        suite_id = _resource_id(path, "/v1/suites/")
        if suite_id is not None:
            self.handle_get_suite(suite_id)
            return
        if path == "/v1/cases":
            self.handle_list_cases(parts.query)
            return
        case_id = _subresource(path, "/v1/cases/", "/instances")
        if case_id is not None:
            self.handle_get_instances(case_id)
            return
        case_id = _subresource(path, "/v1/cases/", "/execution-plan")
        if case_id is not None:
            self.handle_get_execution_plan(case_id)
            return
        case_id = _resource_id(path, "/v1/cases/")
        if case_id is not None:
            self.handle_get_case(case_id)
            return
        if path == "/v1/fixtures":
            self.handle_list_fixtures()
            return
        fixture_id = _resource_id(path, "/v1/fixtures/")
        if fixture_id is not None:
            self.handle_get_fixture(fixture_id)
            return
        if path == "/v1/snapshots":
            self.handle_list_snapshots()
            return
        snapshot_id = _resource_id(path, "/v1/snapshots/")
        if snapshot_id is not None:
            self.handle_get_snapshot(snapshot_id)
            return
        run_id = _subresource(path, "/v1/runs/", "/junit.xml")
        if run_id is not None:
            self.handle_get_run_junit(run_id)
            return
        run_id = _subresource(path, "/v1/runs/", "/coverage")
        if run_id is not None:
            self.handle_get_run_coverage(run_id)
            return
        run_id = _subresource(path, "/v1/runs/", "/diagnostics")
        if run_id is not None:
            self.handle_get_run_diagnostics(run_id)
            return
        run_id = _resource_id(path, "/v1/runs/")
        if run_id is not None:
            self.handle_get_run(run_id)
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def handle_get_suite(self, suite_id: str) -> None:
        try:
            self.send_json(200, self.service.get_suite(suite_id))
        except ApiError as error:
            self.send_api_error(error)

    def handle_list_suites(self) -> None:
        self.send_json(200, self.service.list_suites())

    def handle_get_case(self, case_id: str) -> None:
        try:
            self.send_json(200, self.service.get_case(case_id))
        except ApiError as error:
            self.send_api_error(error)

    def handle_get_instances(self, case_id: str) -> None:
        try:
            self.send_json(200, self.service.get_instances(case_id))
        except ApiError as error:
            self.send_api_error(error)

    def handle_get_execution_plan(self, case_id: str) -> None:
        try:
            self.send_json(200, self.service.get_execution_plan(case_id))
        except ApiError as error:
            self.send_api_error(error)

    def handle_get_fixture(self, fixture_id: str) -> None:
        try:
            self.send_json(200, self.service.get_fixture(fixture_id))
        except ApiError as error:
            self.send_api_error(error)

    def handle_list_fixtures(self) -> None:
        self.send_json(200, self.service.list_fixtures())

    def handle_list_snapshots(self) -> None:
        self.send_json(200, self.service.list_snapshots())

    def handle_get_snapshot(self, snapshot_id: str) -> None:
        try:
            self.send_json(200, self.service.get_snapshot(snapshot_id))
        except ApiError as error:
            self.send_api_error(error)

    def handle_get_run(self, run_id: str) -> None:
        try:
            self.send_json(200, self.service.get_run(run_id))
        except ApiError as error:
            self.send_api_error(error)

    def handle_get_run_junit(self, run_id: str) -> None:
        try:
            report = self.service.get_run_junit(run_id)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_xml(200, render_junit_xml(report))

    def handle_get_run_coverage(self, run_id: str) -> None:
        try:
            report = self.service.get_run_coverage(run_id)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(200, report)

    def handle_get_run_diagnostics(self, run_id: str) -> None:
        try:
            report = self.service.get_run_diagnostics(run_id)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(200, report)

    def handle_list_cases(self, query: str) -> None:
        try:
            filters = self.parse_case_filters(parse_qs(query, keep_blank_values=True))
            self.send_json(200, self.service.list_cases(filters))
        except ApiError as error:
            self.send_api_error(error)

    @staticmethod
    def parse_case_filters(values: dict[str, list[str]]) -> dict[str, object]:
        filters: dict[str, object] = {}

        suite_id = _first(values, "suite_id")
        if suite_id is not None:
            if suite_id == "":
                raise ApiError(400, "validation_error", "suite_id must be a non-empty string")
            filters["suite_id"] = suite_id

        if "include_descendants" in values:
            raw = values["include_descendants"][0]
            if raw not in ("true", "false"):
                raise ApiError(400, "validation_error", "include_descendants must be true or false")
            filters["include_descendants"] = raw == "true"

        kind = _first(values, "kind")
        if kind is not None:
            if kind not in ALLOWED_KINDS:
                raise ApiError(400, "validation_error", f"kind must be one of: {', '.join(ALLOWED_KINDS)}")
            filters["kind"] = kind

        if "enabled" in values:
            raw = values["enabled"][0]
            if raw not in ("true", "false"):
                raise ApiError(400, "validation_error", "enabled must be true or false")
            filters["enabled"] = raw == "true"

        if "tags" in values:
            tags: list[str] = []
            for raw in values["tags"]:
                for tag in raw.split(","):
                    if tag == "":
                        raise ApiError(400, "validation_error", "tags must be non-empty strings")
                    if tag not in tags:
                        tags.append(tag)
            filters["tags"] = tags

        if filters.get("include_descendants") and "suite_id" not in filters:
            raise ApiError(
                400,
                "validation_error",
                "include_descendants can only be used together with suite_id",
            )
        return filters

    # -- POST -----------------------------------------------------------

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/v1/suites":
            self.handle_create_suite()
            return
        if parts.path == "/v1/cases":
            self.handle_create_case()
            return
        if parts.path == "/v1/fixtures":
            self.handle_create_fixture()
            return
        if parts.path == "/v1/assertions/evaluate":
            self.handle_evaluate_assertions()
            return
        if parts.path == "/v1/snapshots":
            self.handle_create_snapshot()
            return
        snapshot_id = _subresource(parts.path, "/v1/snapshots/", "/compare")
        if snapshot_id is not None:
            self.handle_compare_snapshot(snapshot_id)
            return
        if parts.path == "/v1/reports/aggregate":
            self.handle_aggregate_reports()
            return
        if parts.path == "/v1/runs":
            self.handle_create_run()
            return
        run_id = _subresource(parts.path, "/v1/runs/", "/results")
        if run_id is not None:
            self.handle_submit_result(run_id)
            return
        run_id = _subresource(parts.path, "/v1/runs/", "/claims")
        if run_id is not None:
            self.handle_claim_instances(run_id)
            return
        run_id = _subresource(parts.path, "/v1/runs/", "/complete")
        if run_id is not None:
            self.handle_complete_run(run_id)
            return
        run_id = _subresource(parts.path, "/v1/runs/", "/retry")
        if run_id is not None:
            self.handle_retry_run(run_id)
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def handle_create_suite(self) -> None:
        try:
            payload = self.read_body()
            suite = self.service.create_suite(payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(201, suite)

    def handle_create_case(self) -> None:
        try:
            payload = self.read_body()
            case = self.service.create_case(payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(201, case)

    def handle_create_fixture(self) -> None:
        try:
            payload = self.read_body()
            fixture = self.service.create_fixture(payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(201, fixture)

    def handle_evaluate_assertions(self) -> None:
        try:
            payload = self.read_body()
            result = evaluate_assertions(payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(200, result)

    def handle_create_snapshot(self) -> None:
        try:
            payload = self.read_body()
            snapshot = self.service.create_snapshot(payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(201, snapshot)

    def handle_compare_snapshot(self, snapshot_id: str) -> None:
        try:
            payload = self.read_body()
            actual, ignored = validate_compare_request(payload)
            result = self.service.compare_snapshot(snapshot_id, actual, ignored)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(200, result)

    def handle_aggregate_reports(self) -> None:
        try:
            payload = self.read_body()
            report = self.service.aggregate_reports(payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(200, report)

    def handle_create_run(self) -> None:
        try:
            payload = self.read_body()
            run = self.service.create_run(payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(201, run)

    def handle_submit_result(self, run_id: str) -> None:
        try:
            payload = self.read_body()
            report = self.service.submit_result(run_id, payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(200, report)

    def handle_claim_instances(self, run_id: str) -> None:
        try:
            payload = self.read_body()
            claims = self.service.claim_instances(run_id, payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(200, claims)

    def handle_complete_run(self, run_id: str) -> None:
        try:
            report = self.service.complete_run(run_id)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(200, report)

    def handle_retry_run(self, run_id: str) -> None:
        try:
            payload = self.read_body()
            report = self.service.retry_run(run_id, payload)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_json(201, report)

    # -- DELETE ---------------------------------------------------------

    def do_DELETE(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        suite_id = _resource_id(path, "/v1/suites/")
        if suite_id is not None:
            self.handle_delete_suite(suite_id)
            return
        case_id = _resource_id(path, "/v1/cases/")
        if case_id is not None:
            self.handle_delete_case(case_id)
            return
        fixture_id = _resource_id(path, "/v1/fixtures/")
        if fixture_id is not None:
            self.handle_delete_fixture(fixture_id)
            return
        snapshot_id = _resource_id(path, "/v1/snapshots/")
        if snapshot_id is not None:
            self.handle_delete_snapshot(snapshot_id)
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def handle_delete_suite(self, suite_id: str) -> None:
        try:
            self.service.delete_suite(suite_id)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_no_content()

    def handle_delete_case(self, case_id: str) -> None:
        try:
            self.service.delete_case(case_id)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_no_content()

    def handle_delete_fixture(self, fixture_id: str) -> None:
        try:
            self.service.delete_fixture(fixture_id)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_no_content()

    def handle_delete_snapshot(self, snapshot_id: str) -> None:
        try:
            self.service.delete_snapshot(snapshot_id)
        except ApiError as error:
            self.send_api_error(error)
            return
        self.send_no_content()

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
