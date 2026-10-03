"""End-to-end HTTP tests for in-process run records."""

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
        "name": f"name-{case_id}",
        "suite_id": suite_id,
        "kind": "unit",
        "steps": [{"action": "run"}],
    }
    payload.update(overrides)
    return payload


def seed_cases(server: ServerHarness) -> None:
    server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
    server.json("POST", "/v1/cases", case_payload("c1"))
    server.json(
        "POST",
        "/v1/cases",
        case_payload("c2", parameterization={"axes": {"x": [1, 2]}}),
    )
    server.json("POST", "/v1/cases", case_payload("off", enabled=False))


class RunTest(unittest.TestCase):
    def test_create_freezes_instances_and_reports_open(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            status, report = server.json(
                "POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]}
            )
            self.assertEqual(status, 201)
            self.assertEqual(report["id"], "r1")
            self.assertEqual(report["status"], "open")
            self.assertEqual(report["passed"], False)
            self.assertEqual(
                [i["instance_id"] for i in report["instances"]],
                ["c2[0]", "c2[1]", "c1[0]"],
            )
            self.assertEqual(report["instances"][0]["case_name"], "name-c2")
            self.assertEqual(report["instances"][0]["parameters"], {"x": 1})
            self.assertEqual(report["instances"][1]["parameters"], {"x": 2})
            self.assertEqual(report["instances"][2]["case_name"], "name-c1")
            self.assertEqual(report["instances"][2]["parameters"], {})
            self.assertTrue(all(i["outcome"] == "pending" for i in report["instances"]))
            self.assertEqual(
                report["summary"],
                {
                    "total": 3,
                    "pending": 3,
                    "passed": 0,
                    "failed": 0,
                    "error": 0,
                    "skipped": 0,
                    "duration_ms": 0,
                },
            )

            # Later catalog changes do not affect the frozen record.
            server.request("DELETE", "/v1/cases/c2")
            status, fetched = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(fetched, report)

    def test_create_validation_and_conflicts(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            bad_payloads = [
                {"id": "r", "case_ids": ["c1"], "extra": 1},
                {"id": "  ", "case_ids": ["c1"]},
                {"case_ids": ["c1"]},
                {"id": "r"},
                {"id": "r", "case_ids": []},
                {"id": "r", "case_ids": ["c1", "c1"]},
                {"id": "r", "case_ids": ["c1", 2]},
                {"id": "r", "case_ids": "c1"},
            ]
            for payload in bad_payloads:
                status, body = server.json("POST", "/v1/runs", payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(body["error"]["code"], "validation_error", payload)

            status, body = server.json(
                "POST", "/v1/runs", {"id": "r", "case_ids": [f"c{i}" for i in range(101)]}
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            status, body = server.json("POST", "/v1/runs", raw_body=b"{nope")
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

            status, body = server.json(
                "POST", "/v1/runs", {"id": "r", "case_ids": ["ghost"]}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "case_not_found")

            status, body = server.json(
                "POST", "/v1/runs", {"id": "r", "case_ids": ["c1", "off"]}
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "case_disabled")

            server.json("POST", "/v1/runs", {"id": "r", "case_ids": ["c1"]})
            status, body = server.json(
                "POST", "/v1/runs", {"id": "r", "case_ids": ["c1"]}
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_exists")

            # Failed creations left no runs behind.
            status, body = server.json("GET", "/v1/runs/ghost")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

    def test_instance_limit(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.json(
                "POST",
                "/v1/cases",
                case_payload("big", parameterization={"rows": [{"x": i} for i in range(1000)]}),
            )
            status, body = server.json(
                "POST", "/v1/runs", {"id": "r", "case_ids": ["big"] * 1}
            )
            self.assertEqual(status, 201)
            server.json(
                "POST",
                "/v1/cases",
                case_payload("b2", parameterization={"rows": [{"x": i} for i in range(1000)]}),
            )
            too_many = ["big", "b2", "big2", "big3", "big4", "big5"]
            for cid in ("big2", "big3", "big4", "big5"):
                server.json(
                    "POST",
                    "/v1/cases",
                    case_payload(cid, parameterization={"rows": [{"x": i} for i in range(1000)]}),
                )
            status, body = server.json(
                "POST", "/v1/runs", {"id": "r2", "case_ids": too_many}
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
            status, body = server.json("GET", "/v1/runs/r2")
            self.assertEqual(status, 404)

    def test_result_submission_and_completion(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})

            status, report = server.json(
                "POST",
                "/v1/runs/r1/results",
                {
                    "instance_id": "c2[0]",
                    "outcome": "passed",
                    "duration_ms": 12,
                    "details": {"log": ["a", 1, None]},
                },
            )
            self.assertEqual(status, 200)
            first = report["instances"][0]
            self.assertEqual(first["outcome"], "passed")
            self.assertEqual(first["duration_ms"], 12)
            self.assertEqual(first["details"], {"log": ["a", 1, None]})
            self.assertEqual(report["summary"]["pending"], 2)
            self.assertEqual(report["summary"]["passed"], 1)
            self.assertEqual(report["summary"]["duration_ms"], 12)

            # Completing with pending instances fails.
            status, body = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # Duplicate submission fails.
            status, body = server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c2[0]", "outcome": "failed", "duration_ms": 1},
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "result_exists")

            server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c2[1]", "outcome": "skipped", "duration_ms": 0},
            )
            status, report = server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 3},
            )
            self.assertNotIn("details", report["instances"][2])
            self.assertEqual(report["summary"]["duration_ms"], 15)
            self.assertEqual(report["summary"]["skipped"], 1)

            status, report = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 200)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["passed"], True)

            # Repeated completion and late submissions are conflicts.
            status, body = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_completed")
            status, body = server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c1[0]", "outcome": "failed", "duration_ms": 1},
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_completed")

    def test_failed_run_is_not_passed(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c2[0]", "outcome": "failed", "duration_ms": 5},
            )
            server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c2[1]", "outcome": "error", "duration_ms": 7},
            )
            status, report = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 200)
            self.assertEqual(report["passed"], False)
            self.assertEqual(report["summary"]["failed"], 1)
            self.assertEqual(report["summary"]["error"], 1)
            self.assertEqual(report["summary"]["duration_ms"], 12)

    def test_result_validation_and_missing_resources(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

            bad_payloads = [
                {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 1, "x": 1},
                {"outcome": "passed", "duration_ms": 1},
                {"instance_id": "c1[0]", "duration_ms": 1},
                {"instance_id": "c1[0]", "outcome": "passed"},
                {"instance_id": "c1[0]", "outcome": "pending", "duration_ms": 1},
                {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": -1},
                {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 1.5},
                {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": True},
                {"instance_id": "", "outcome": "passed", "duration_ms": 1},
            ]
            for payload in bad_payloads:
                status, body = server.json("POST", "/v1/runs/r1/results", payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(body["error"]["code"], "validation_error", payload)

            status, body = server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c2[0]", "outcome": "passed", "duration_ms": 1},
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "instance_not_found")

            status, body = server.json(
                "POST",
                "/v1/runs/ghost/results",
                {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 1},
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            status, body = server.json("POST", "/v1/runs/ghost/complete")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            # Failed submissions left no partial results.
            _, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(report["summary"]["pending"], 1)
            self.assertEqual(report["instances"][0]["outcome"], "pending")

    def test_restart_clears_runs(self) -> None:
        service = Service()
        with ServerHarness(service) as server:
            seed_cases(server)
            status, _ = server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            self.assertEqual(status, 201)
        with ServerHarness(Service()) as server:
            status, body = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")


if __name__ == "__main__":
    unittest.main()
