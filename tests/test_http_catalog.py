"""End-to-end HTTP tests for the suite/case catalog routes."""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from testlattice.server import Handler
from testlattice.service import Service


def make_handler(service: Service) -> type[Handler]:
    class _Handler(Handler):
        pass

    _Handler.service = service
    return _Handler


class ServerHarness:
    def __init__(self, service: Service | None = None) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service or Service()))
        self.host, self.port = self.httpd.server_address
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "ServerHarness":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def request(
        self, method: str, path: str, body: object = ..., raw_body: bytes | None = None
    ) -> tuple[int, str, bytes]:
        conn = HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        payload: bytes | None = None
        if raw_body is not None:
            payload = raw_body
            headers["Content-Type"] = "application/json"
        elif body is not ...:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if payload is not None:
            headers["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        content_type = response.getheader("Content-Type", "")
        conn.close()
        return response.status, content_type, data

    def json(
        self, method: str, path: str, body: object = ..., raw_body: bytes | None = None
    ) -> tuple[int, dict | list]:
        status, _, data = self.request(method, path, body, raw_body=raw_body)
        return status, json.loads(data)


def case_payload(case_id: str = "c1", suite_id: str = "s1", **overrides: object) -> dict:
    payload: dict = {
        "id": case_id,
        "name": "case",
        "suite_id": suite_id,
        "kind": "unit",
        "steps": [{"action": "run"}],
    }
    payload.update(overrides)
    return payload


class HttpTest(unittest.TestCase):
    def test_healthz_unchanged(self) -> None:
        with ServerHarness() as server:
            status, content_type, data = server.request("GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertTrue(content_type.startswith("application/json"))
            payload = json.loads(data)
            self.assertEqual(payload["status"], "ok")

    def test_unknown_route_keeps_baseline_404(self) -> None:
        with ServerHarness() as server:
            status, _, data = server.request("GET", "/nope")
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(data)["error"]["code"], "not_found")
            status, _, _ = server.request("POST", "/v1/suites/x", body={})
            self.assertEqual(status, 404)
            # Unsupported methods keep the http.server baseline behavior.
            status, _, _ = server.request("PUT", "/v1/cases", body={})
            self.assertEqual(status, 501)

    def test_suite_lifecycle_status_codes(self) -> None:
        with ServerHarness() as server:
            status, content_type, data = server.request("POST", "/v1/suites", {"id": "r", "name": "R"})
            self.assertEqual(status, 201)
            self.assertTrue(content_type.startswith("application/json"))
            self.assertEqual(json.loads(data)["parent_id"], None)

            status, suite = server.json("GET", "/v1/suites/r")
            self.assertEqual(status, 200)
            self.assertEqual(suite["id"], "r")

            status, suites = server.json("GET", "/v1/suites")
            self.assertEqual(status, 200)
            self.assertEqual(suites, [suite])

            status, content_type, data = server.request("DELETE", "/v1/suites/r")
            self.assertEqual(status, 204)
            self.assertEqual(data, b"")
            self.assertNotIn("application/json", content_type)

            status, body = server.json("GET", "/v1/suites/r")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "suite_not_found")

    def test_case_lifecycle_and_filters(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.json(
                "POST",
                "/v1/suites",
                {"id": "s2", "name": "S2", "parent_id": "s1"},
            )
            status, created = server.json(
                "POST",
                "/v1/cases",
                case_payload("c1", "s1", kind="api", tags=["smoke", "reg"]),
            )
            self.assertEqual(status, 201)
            self.assertEqual(created["tags"], ["smoke", "reg"])
            self.assertEqual(created["timeout_seconds"], 300)
            server.json("POST", "/v1/cases", case_payload("c2", "s2", enabled=False))

            status, case = server.json("GET", "/v1/cases/c1")
            self.assertEqual(status, 200)
            self.assertEqual(case["name"], "case")

            status, cases = server.json("GET", "/v1/cases")
            self.assertEqual([c["id"] for c in cases], ["c1", "c2"])

            _, cases = server.json("GET", "/v1/cases?suite_id=s1")
            self.assertEqual([c["id"] for c in cases], ["c1"])
            _, cases = server.json("GET", "/v1/cases?suite_id=s1&include_descendants=true")
            self.assertEqual([c["id"] for c in cases], ["c1", "c2"])
            _, cases = server.json("GET", "/v1/cases?kind=api")
            self.assertEqual([c["id"] for c in cases], ["c1"])
            _, cases = server.json("GET", "/v1/cases?enabled=false")
            self.assertEqual([c["id"] for c in cases], ["c2"])
            _, cases = server.json("GET", "/v1/cases?tags=smoke&tags=reg")
            self.assertEqual([c["id"] for c in cases], ["c1"])
            _, cases = server.json("GET", "/v1/cases?tags=missing")
            self.assertEqual(cases, [])

            status, _, _ = server.request("DELETE", "/v1/cases/c1")
            self.assertEqual(status, 204)
            status, body = server.json("GET", "/v1/cases/c1")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "case_not_found")
            _, cases = server.json("GET", "/v1/cases")
            self.assertEqual([c["id"] for c in cases], ["c2"])

    def test_error_codes(self) -> None:
        with ServerHarness() as server:
            status, body = server.json("POST", "/v1/suites", raw_body=b"{not json")
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")
            status, body = server.json("POST", "/v1/suites", raw_body=b"")
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

            status, body = server.json("POST", "/v1/suites", {"id": "  ", "name": "x"})
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            server.json("POST", "/v1/suites", {"id": "r", "name": "R"})
            status, body = server.json("POST", "/v1/suites", {"id": "r", "name": "R2"})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "suite_exists")

            status, body = server.json("POST", "/v1/cases", case_payload(suite_id="ghost"))
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "suite_not_found")

            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.json("POST", "/v1/cases", case_payload())
            status, body = server.json("POST", "/v1/cases", case_payload())
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "case_exists")

            server.json(
                "POST", "/v1/suites", {"id": "child", "name": "C", "parent_id": "r"}
            )
            status, body = server.json("DELETE", "/v1/suites/r")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "suite_not_empty")

            # Failed creation leaves no trace.
            _, suites = server.json("GET", "/v1/suites")
            self.assertEqual({s["id"] for s in suites}, {"r", "child", "s1"})

    def test_bad_filters(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            for query in (
                "kind=nope",
                "enabled=1",
                "include_descendants=yes",
                "include_descendants=true",
                "tags=",
            ):
                status, body = server.json("GET", f"/v1/cases?{query}")
                self.assertEqual(status, 400, query)
                self.assertEqual(body["error"]["code"], "validation_error", query)
            status, body = server.json("GET", "/v1/cases?suite_id=ghost")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "suite_not_found")

    def test_restart_clears_data(self) -> None:
        service = Service()
        with ServerHarness(service) as server:
            status, _ = server.json("POST", "/v1/suites", {"id": "r", "name": "R"})
            self.assertEqual(status, 201)
        with ServerHarness(Service()) as server:
            status, suites = server.json("GET", "/v1/suites")
            self.assertEqual(status, 200)
            self.assertEqual(suites, [])


if __name__ == "__main__":
    unittest.main()
