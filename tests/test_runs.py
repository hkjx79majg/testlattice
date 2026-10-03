"""Tests for in-process run records and external result submission."""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from testlattice.server import Handler
from testlattice.service import (
    MAX_RUN_CASE_IDS,
    MAX_RUN_INSTANCES,
    ApiError,
    Service,
)


def suite_payload(suite_id: str = "s1") -> dict:
    return {"id": suite_id, "name": f"Suite {suite_id}"}


def case_payload(case_id: str = "c1", suite_id: str = "s1", **overrides: object) -> dict:
    payload: dict = {
        "id": case_id,
        "name": f"case {case_id}",
        "suite_id": suite_id,
        "kind": "unit",
        "steps": [{"action": "run"}],
    }
    payload.update(overrides)
    return payload


class RunServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.create_suite(suite_payload())
        self.service.create_case(
            case_payload("c1", parameterization={"axes": {"a": [1, 2]}})
        )
        self.service.create_case(case_payload("c2"))

    def create_run(self, run_id: str = "r1", case_ids: list[str] | None = None) -> dict:
        return self.service.create_run(
            {"id": run_id, "case_ids": case_ids if case_ids is not None else ["c1", "c2"]}
        )

    def submit(self, run_id: str, instance_id: str, **overrides: object) -> dict:
        payload: dict = {
            "instance_id": instance_id,
            "outcome": "passed",
            "duration_ms": 10,
        }
        payload.update(overrides)
        return self.service.submit_result(run_id, payload)

    # -- creation / report -------------------------------------------------

    def test_create_freezes_instances_in_declaration_order(self) -> None:
        report = self.create_run()
        self.assertEqual(report["id"], "r1")
        self.assertEqual(report["status"], "open")
        self.assertFalse(report["passed"])
        self.assertEqual(
            [item["instance_id"] for item in report["instances"]],
            ["c1[0]", "c1[1]", "c2[0]"],
        )
        first = report["instances"][0]
        self.assertEqual(first["case_id"], "c1")
        self.assertEqual(first["name"], "case c1")
        self.assertEqual(first["parameters"], {"a": 1})
        self.assertEqual(report["instances"][1]["parameters"], {"a": 2})
        self.assertEqual(report["instances"][2]["parameters"], {})
        for item in report["instances"]:
            self.assertEqual(item["status"], "pending")
            self.assertNotIn("outcome", item)
            self.assertNotIn("duration_ms", item)
            self.assertNotIn("details", item)
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

    def test_get_returns_same_report(self) -> None:
        created = self.create_run()
        self.assertEqual(self.service.get_run("r1"), created)

    def test_run_id_is_trimmed(self) -> None:
        report = self.service.create_run({"id": "  r1  ", "case_ids": ["c2"]})
        self.assertEqual(report["id"], "r1")
        self.assertEqual(self.service.get_run("r1")["id"], "r1")

    def test_report_is_detached_from_stored_state(self) -> None:
        report = self.create_run()
        report["instances"][0]["parameters"]["a"] = 99
        report["summary"]["total"] = 0
        again = self.service.get_run("r1")
        self.assertEqual(again["instances"][0]["parameters"], {"a": 1})
        self.assertEqual(again["summary"]["total"], 3)

    def test_catalog_changes_after_creation_do_not_affect_run(self) -> None:
        self.create_run()
        # Delete and rebuild c1/c2 with different names and shapes.
        self.service.delete_case("c2")
        self.service.delete_case("c1")
        self.service.create_case(
            case_payload("c1", name="renamed", parameterization={"rows": [{"z": 0}]})
        )
        self.service.create_case(case_payload("c2", name="also renamed"))
        report = self.service.get_run("r1")
        self.assertEqual(
            [item["instance_id"] for item in report["instances"]],
            ["c1[0]", "c1[1]", "c2[0]"],
        )
        self.assertEqual(report["instances"][0]["name"], "case c1")
        self.assertEqual(report["instances"][0]["parameters"], {"a": 1})
        self.assertEqual(report["instances"][2]["name"], "case c2")
        # A newly created run over the rebuilt cases sees the new shape.
        fresh = self.service.create_run({"id": "r2", "case_ids": ["c1", "c2"]})
        self.assertEqual(
            [item["instance_id"] for item in fresh["instances"]], ["c1[0]", "c2[0]"]
        )
        self.assertEqual(fresh["instances"][0]["parameters"], {"z": 0})

    # -- validation --------------------------------------------------------

    def test_invalid_run_payloads(self) -> None:
        for bad in (
            None,
            [],
            "x",
            42,
            {},
            {"case_ids": ["c1"]},
            {"id": "r1"},
            {"id": "r1", "case_ids": ["c1"], "extra": 1},
            {"id": "", "case_ids": ["c1"]},
            {"id": "   ", "case_ids": ["c1"]},
            {"id": 1, "case_ids": ["c1"]},
            {"id": "r1", "case_ids": []},
            {"id": "r1", "case_ids": "c1"},
            {"id": "r1", "case_ids": 1},
            {"id": "r1", "case_ids": ["c1", 1]},
            {"id": "r1", "case_ids": [""]},
            {"id": "r1", "case_ids": ["c1", "c1"]},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ApiError) as caught:
                    self.service.create_run(bad)
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(caught.exception.code, "validation_error")

    def test_case_id_count_boundaries(self) -> None:
        for index in range(MAX_RUN_CASE_IDS):
            self.service.create_case(case_payload(f"x{index}"))
        ids = [f"x{index}" for index in range(MAX_RUN_CASE_IDS)]
        report = self.service.create_run({"id": "ok", "case_ids": ids})
        self.assertEqual(report["summary"]["total"], MAX_RUN_CASE_IDS)
        with self.assertRaises(ApiError) as caught:
            self.service.create_run({"id": "over", "case_ids": ids + ["c1"]})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "validation_error")
        with self.assertRaises(ApiError):
            self.service.get_run("over")

    def test_instance_limit(self) -> None:
        # Each case still caps at 1000 instances; the run-level cap of 5000
        # is reached by combining frozen instances across cases.
        for index in range(6):
            self.service.create_case(
                case_payload(
                    f"b{index}",
                    parameterization={"axes": {"n": list(range(1000))}},
                )
            )
        five = [f"b{index}" for index in range(5)]
        report = self.service.create_run({"id": "at-limit", "case_ids": five})
        self.assertEqual(report["summary"]["total"], MAX_RUN_INSTANCES)

        with self.assertRaises(ApiError) as caught:
            self.service.create_run({"id": "over", "case_ids": five + ["b5"]})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "validation_error")
        with self.assertRaises(ApiError):
            self.service.get_run("over")

    def test_unknown_case_404_and_disabled_case_409(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.create_run({"id": "r", "case_ids": ["ghost"]})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "case_not_found")

        self.service.create_case(case_payload("off", enabled=False))
        with self.assertRaises(ApiError) as caught:
            self.service.create_run({"id": "r", "case_ids": ["off"]})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "case_disabled")

        # A disabled/unknown case later in the list does not leave a partial run.
        with self.assertRaises(ApiError):
            self.service.create_run({"id": "r", "case_ids": ["c1", "off"]})
        with self.assertRaises(ApiError):
            self.service.create_run({"id": "r2", "case_ids": ["ghost", "c1"]})
        with self.assertRaises(ApiError) as caught:
            self.service.get_run("r")
        self.assertEqual(caught.exception.code, "run_not_found")
        with self.assertRaises(ApiError) as caught:
            self.service.get_run("r2")
        self.assertEqual(caught.exception.code, "run_not_found")

    def test_run_id_conflict(self) -> None:
        self.create_run("dup")
        with self.assertRaises(ApiError) as caught:
            self.create_run("dup", ["c2"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "run_exists")
        # The original run is untouched.
        self.assertEqual(
            [item["instance_id"] for item in self.service.get_run("dup")["instances"]],
            ["c1[0]", "c1[1]", "c2[0]"],
        )

    def test_failed_creation_leaves_no_run(self) -> None:
        with self.assertRaises(ApiError):
            self.service.create_run({"id": "r", "case_ids": ["ghost"]})
        with self.assertRaises(ApiError) as caught:
            self.service.get_run("r")
        self.assertEqual(caught.exception.code, "run_not_found")

    def test_get_unknown_run_404(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.get_run("ghost")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "run_not_found")

    # -- results -----------------------------------------------------------

    def test_submit_updates_instance_and_summary(self) -> None:
        self.create_run()
        report = self.submit("r1", "c1[0]", outcome="failed", duration_ms=25, details={"x": 1})
        item = report["instances"][0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["outcome"], "failed")
        self.assertEqual(item["duration_ms"], 25)
        self.assertEqual(item["details"], {"x": 1})
        self.assertEqual(report["instances"][1]["status"], "pending")
        self.assertNotIn("outcome", report["instances"][1])
        self.assertEqual(
            report["summary"],
            {
                "total": 3,
                "pending": 2,
                "passed": 0,
                "failed": 1,
                "error": 0,
                "skipped": 0,
                "duration_ms": 25,
            },
        )

        report = self.submit("r1", "c1[1]", outcome="skipped", duration_ms=0)
        self.assertNotIn("details", report["instances"][1])
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertEqual(report["summary"]["duration_ms"], 25)

        report = self.submit("r1", "c2[0]", outcome="error", duration_ms=5, details=None)
        self.assertIsNone(report["instances"][2]["details"])
        self.assertEqual(report["summary"]["error"], 1)
        self.assertEqual(report["summary"]["duration_ms"], 30)
        self.assertEqual(report["summary"]["pending"], 0)

    def test_details_accept_any_json_value(self) -> None:
        self.create_run(case_ids=["c2"])
        for details in (
            None,
            True,
            42,
            "note",
            [1, "x", None],
            {"nested": [1, 2, {"k": "v"}]},
        ):
            run_id = f"run-{type(details).__name__}"
            self.service.create_run({"id": run_id, "case_ids": ["c2"]})
            report = self.submit(run_id, "c2[0]", details=details)
            self.assertEqual(report["instances"][0]["details"], details)

    def test_stored_details_are_detached(self) -> None:
        self.create_run(case_ids=["c2"])
        details = {"nested": [1, 2, {"k": "v"}]}
        report = self.submit("r1", "c2[0]", details=details)
        report["instances"][0]["details"]["nested"].append(3)
        again = self.service.get_run("r1")
        self.assertEqual(again["instances"][0]["details"], details)

    def test_invalid_result_payloads(self) -> None:
        self.create_run()
        for bad in (
            None,
            [],
            {},
            {"outcome": "passed", "duration_ms": 1},
            {"instance_id": "c1[0]", "duration_ms": 1},
            {"instance_id": "c1[0]", "outcome": "passed"},
            {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 1, "x": 0},
            {"instance_id": "", "outcome": "passed", "duration_ms": 1},
            {"instance_id": 1, "outcome": "passed", "duration_ms": 1},
            {"instance_id": "c1[0]", "outcome": "nope", "duration_ms": 1},
            {"instance_id": "c1[0]", "outcome": 1, "duration_ms": 1},
            {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": -1},
            {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 1.5},
            {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": "1"},
            {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": True},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ApiError) as caught:
                    self.service.submit_result("r1", bad)
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(caught.exception.code, "validation_error")
        # Failed submissions leave no partial result.
        self.assertEqual(self.service.get_run("r1")["summary"]["pending"], 3)

    def test_submit_to_unknown_run_or_instance(self) -> None:
        self.create_run()
        payload = {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 1}
        with self.assertRaises(ApiError) as caught:
            self.service.submit_result("ghost", payload)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "run_not_found")

        with self.assertRaises(ApiError) as caught:
            self.service.submit_result(
                "r1",
                {"instance_id": "c9[0]", "outcome": "passed", "duration_ms": 1},
            )
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "instance_not_found")

        # An instance whose case is not part of this run is not in the run.
        self.service.create_case(case_payload("c3"))
        with self.assertRaises(ApiError) as caught:
            self.service.submit_result(
                "r1",
                {"instance_id": "c3[0]", "outcome": "passed", "duration_ms": 1},
            )
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "instance_not_found")

    def test_duplicate_result_conflict_keeps_original(self) -> None:
        self.create_run(case_ids=["c2"])
        self.submit("r1", "c2[0]")
        with self.assertRaises(ApiError) as caught:
            self.submit("r1", "c2[0]", outcome="failed", duration_ms=99)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "result_exists")
        item = self.service.get_run("r1")["instances"][0]
        self.assertEqual(item["outcome"], "passed")
        self.assertEqual(item["duration_ms"], 10)

    # -- completion --------------------------------------------------------

    def finish(self, run_id: str, outcomes: dict[str, str]) -> None:
        for item in self.service.get_run(run_id)["instances"]:
            self.submit(run_id, item["instance_id"], outcome=outcomes[item["instance_id"]])

    def test_complete_requires_no_pending(self) -> None:
        self.create_run(case_ids=["c2"])
        with self.assertRaises(ApiError) as caught:
            self.service.complete_run("r1")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "run_incomplete")
        self.assertEqual(self.service.get_run("r1")["status"], "open")

    def test_complete_lifecycle_and_passed_flag(self) -> None:
        self.create_run()
        self.finish(
            "r1",
            {"c1[0]": "passed", "c1[1]": "skipped", "c2[0]": "passed"},
        )
        report = self.service.complete_run("r1")
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["passed"])

        with self.assertRaises(ApiError) as caught:
            self.service.complete_run("r1")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "run_completed")

        with self.assertRaises(ApiError) as caught:
            self.submit("r1", "c1[0]")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "run_completed")

    def test_passed_false_with_failure_or_error(self) -> None:
        for outcome in ("failed", "error"):
            run_id = f"run-{outcome}"
            self.service.create_run({"id": run_id, "case_ids": ["c2"]})
            self.submit(run_id, "c2[0]", outcome=outcome)
            report = self.service.complete_run(run_id)
            self.assertEqual(report["status"], "completed")
            self.assertFalse(report["passed"])

    def test_passed_false_while_open_even_without_failures(self) -> None:
        self.create_run(case_ids=["c2"])
        self.submit("r1", "c2[0]", outcome="passed")
        self.assertFalse(self.service.get_run("r1")["passed"])

    def test_complete_unknown_run_404(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.complete_run("ghost")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "run_not_found")


class RunHttpTest(unittest.TestCase):
    class Harness:
        def __init__(self, service: Service | None = None) -> None:
            class _Handler(Handler):
                pass

            _Handler.service = service or Service()
            self.service = _Handler.service
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            self.host, self.port = self.httpd.server_address
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

        def __enter__(self) -> "RunHttpTest.Harness":
            self.thread.start()
            return self

        def __exit__(self, *exc: object) -> None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join()

        def request(
            self, method: str, path: str, body: object = ..., raw_body: bytes | None = None
        ) -> tuple[int, object]:
            conn = HTTPConnection(self.host, self.port, timeout=5)
            headers = {}
            payload = None
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
            status = response.status
            conn.close()
            return status, json.loads(data) if data else None

    def seed(self, harness: "RunHttpTest.Harness") -> None:
        harness.request("POST", "/v1/suites", {"id": "s1", "name": "S1"})
        harness.request(
            "POST",
            "/v1/cases",
            case_payload("c1", parameterization={"axes": {"a": [1, 2]}}),
        )
        harness.request("POST", "/v1/cases", case_payload("c2", enabled=False))

    def result_payload(self, instance_id: str, **overrides: object) -> dict:
        payload: dict = {
            "instance_id": instance_id,
            "outcome": "passed",
            "duration_ms": 1,
        }
        payload.update(overrides)
        return payload

    def test_full_run_flow_over_http(self) -> None:
        with self.Harness() as server:
            self.seed(server)
            status, created = server.request(
                "POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]}
            )
            self.assertEqual(status, 201)
            self.assertEqual(created["status"], "open")
            self.assertFalse(created["passed"])
            self.assertEqual(
                [item["instance_id"] for item in created["instances"]],
                ["c1[0]", "c1[1]"],
            )
            self.assertTrue(all(item["status"] == "pending" for item in created["instances"]))

            status, fetched = server.request("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(fetched, created)

            status, report = server.request(
                "POST", "/v1/runs/r1/results",
                self.result_payload("c1[0]", duration_ms=12),
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["instances"][0]["status"], "passed")
            self.assertEqual(report["summary"]["pending"], 1)
            self.assertEqual(report["summary"]["duration_ms"], 12)

            status, body = server.request("POST", "/v1/runs/r1/complete", {})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            status, report = server.request(
                "POST", "/v1/runs/r1/results",
                self.result_payload("c1[1]", outcome="failed", duration_ms=3),
            )
            self.assertEqual(status, 200)

            # Completion ignores its body: JSON object, empty body, garbage.
            status, report = server.request("POST", "/v1/runs/r1/complete", {})
            self.assertEqual(status, 200)
            self.assertEqual(report["status"], "completed")
            self.assertFalse(report["passed"])

            status, body = server.request(
                "POST", "/v1/runs/r1/complete", raw_body=b"not json"
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_completed")

            status, report = server.request("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["summary"]["duration_ms"], 15)

    def test_create_http_error_codes(self) -> None:
        with self.Harness() as server:
            self.seed(server)

            status, body = server.request("POST", "/v1/runs", raw_body=b"{bad")
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

            status, body = server.request(
                "POST", "/v1/runs", {"id": "r", "case_ids": ["c1", "c1"]}
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            status, body = server.request(
                "POST", "/v1/runs", {"id": "r", "case_ids": ["ghost"]}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "case_not_found")

            status, body = server.request(
                "POST", "/v1/runs", {"id": "r", "case_ids": ["c2"]}
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "case_disabled")

            server.request("POST", "/v1/runs", {"id": "r", "case_ids": ["c1"]})
            status, body = server.request(
                "POST", "/v1/runs", {"id": "r", "case_ids": ["c1"]}
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_exists")

            # The conflict leaves the original run (with c1) untouched.
            status, body = server.request("GET", "/v1/runs/r")
            self.assertEqual(status, 200)
            self.assertEqual(
                [item["instance_id"] for item in body["instances"]],
                ["c1[0]", "c1[1]"],
            )

    def test_results_http_error_codes(self) -> None:
        with self.Harness() as server:
            self.seed(server)
            server.request("POST", "/v1/runs", {"id": "r", "case_ids": ["c1"]})

            status, body = server.request(
                "POST", "/v1/runs/r/results", raw_body=b"{bad"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

            status, body = server.request(
                "POST", "/v1/runs/r/results",
                {"instance_id": "c1[0]", "outcome": "passed"},  # duration_ms missing
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            status, body = server.request("GET", "/v1/runs/ghost")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            status, body = server.request(
                "POST", "/v1/runs/ghost/results", self.result_payload("c1[0]")
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            status, body = server.request(
                "POST", "/v1/runs/r/results", self.result_payload("c9[0]")
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "instance_not_found")

            status, _ = server.request(
                "POST", "/v1/runs/r/results", self.result_payload("c1[0]")
            )
            self.assertEqual(status, 200)
            status, body = server.request(
                "POST", "/v1/runs/r/results", self.result_payload("c1[0]")
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "result_exists")

            status, body = server.request("POST", "/v1/runs/ghost/complete", {})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

    def test_completed_run_rejects_submission(self) -> None:
        with self.Harness() as server:
            self.seed(server)
            server.request("POST", "/v1/runs", {"id": "r", "case_ids": ["c1"]})
            for instance_id in ("c1[0]", "c1[1]"):
                status, _ = server.request(
                    "POST", "/v1/runs/r/results", self.result_payload(instance_id)
                )
                self.assertEqual(status, 200)
            status, _ = server.request("POST", "/v1/runs/r/complete", {})
            self.assertEqual(status, 200)
            status, body = server.request(
                "POST", "/v1/runs/r/results", self.result_payload("c1[0]")
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_completed")

    def test_collection_and_deep_subpaths_are_not_routes(self) -> None:
        with self.Harness() as server:
            self.seed(server)
            server.request("POST", "/v1/runs", {"id": "r", "case_ids": ["c1"]})

            # GET /v1/runs is not a collection endpoint.
            status, body = server.request("GET", "/v1/runs")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")

            for path in (
                "/v1/runs/r/results/extra",
                "/v1/runs/r/complete/extra",
                "/v1/runs/r/other",
            ):
                status, body = server.request("POST", path, {})
                self.assertEqual(status, 404, path)
                self.assertEqual(body["error"]["code"], "not_found", path)
                status, body = server.request("GET", path)
                self.assertEqual(status, 404, path)
                self.assertEqual(body["error"]["code"], "not_found", path)

    def test_runs_are_in_memory_only(self) -> None:
        service = Service()
        with self.Harness(service) as server:
            server.request("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.request("POST", "/v1/cases", case_payload("c1"))
            status, _ = server.request(
                "POST", "/v1/runs", {"id": "r", "case_ids": ["c1"]}
            )
            self.assertEqual(status, 201)
        with self.Harness(Service()) as fresh:
            status, body = fresh.request("GET", "/v1/runs/r")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")


if __name__ == "__main__":
    unittest.main()
