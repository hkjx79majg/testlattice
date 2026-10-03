"""End-to-end HTTP tests for run retries."""

from __future__ import annotations

import unittest

import testlattice.service as service_module
from test_runs import ServerHarness, case_payload, seed_cases


def submit(server: ServerHarness, run_id: str, instance_id: str, outcome: str, **extra: object) -> tuple[int, dict]:
    payload = {"instance_id": instance_id, "outcome": outcome, "duration_ms": 5}
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def finish_with(server: ServerHarness, run_id: str, results: dict[str, str]) -> dict:
    for instance_id, outcome in results.items():
        status, _ = submit(server, run_id, instance_id, outcome, details={"why": outcome})
        assert status == 200, (instance_id, outcome, status)
    status, report = server.json("POST", f"/v1/runs/{run_id}/complete")
    assert status == 200
    return report


class RetryTest(unittest.TestCase):
    def test_default_retry_freezes_failed_and_error_instances(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish_with(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "error"},
            )

            status, report = server.json(
                "POST", "/v1/runs/r1/retry", {"id": " r2 "}
            )
            self.assertEqual(status, 201)
            self.assertEqual(report["id"], "r2")
            self.assertEqual(report["status"], "open")
            self.assertEqual(report["passed"], False)
            self.assertEqual(report["retry_of"], "r1")
            self.assertEqual(report["root_run_id"], "r1")
            self.assertEqual(report["attempt"], 1)
            # Relative frozen order; passed instance is not selected.
            self.assertEqual(
                [i["instance_id"] for i in report["instances"]],
                ["c2[1]", "c1[0]"],
            )
            first = report["instances"][0]
            self.assertEqual(first["case_name"], "name-c2")
            self.assertEqual(first["parameters"], {"x": 2})
            second = report["instances"][1]
            self.assertEqual(second["case_name"], "name-c1")
            self.assertEqual(second["parameters"], {})
            for instance in report["instances"]:
                self.assertEqual(instance["outcome"], "pending")
                self.assertNotIn("duration_ms", instance)
                self.assertNotIn("details", instance)
            self.assertEqual(
                report["summary"],
                {
                    "total": 2,
                    "pending": 2,
                    "passed": 0,
                    "failed": 0,
                    "error": 0,
                    "skipped": 0,
                    "duration_ms": 0,
                },
            )

            status, fetched = server.json("GET", "/v1/runs/r2")
            self.assertEqual(status, 200)
            self.assertEqual(fetched, report)

    def test_normal_run_report_has_no_retry_fields(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            status, report = server.json(
                "POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]}
            )
            self.assertEqual(status, 201)
            self.assertNotIn("retry_of", report)
            self.assertNotIn("root_run_id", report)
            self.assertNotIn("attempt", report)
            finish_with(server, "r1", {"c1[0]": "passed"})
            status, fetched = server.json("GET", "/v1/runs/r1")
            self.assertNotIn("retry_of", fetched)
            self.assertNotIn("root_run_id", fetched)
            self.assertNotIn("attempt", fetched)

    def test_retry_ignores_later_catalog_changes(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish_with(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "error"},
            )

            # Delete, recreate and disable cases after the source finished;
            # the retry still uses the frozen source report.
            server.request("DELETE", "/v1/cases/c2")
            server.request("DELETE", "/v1/cases/c1")
            server.json("POST", "/v1/cases", case_payload("c1", name="CHANGED"))
            server.json(
                "POST",
                "/v1/cases",
                case_payload("c2", name="CHANGED-C2", enabled=False),
            )

            status, report = server.json(
                "POST", "/v1/runs/r1/retry", {"id": "r2"}
            )
            self.assertEqual(status, 201)
            self.assertEqual(
                [i["instance_id"] for i in report["instances"]],
                ["c2[1]", "c1[0]"],
            )
            self.assertEqual(report["instances"][0]["case_name"], "name-c2")
            self.assertEqual(report["instances"][0]["parameters"], {"x": 2})
            self.assertEqual(report["instances"][1]["case_name"], "name-c1")

    def test_explicit_outcomes_filter(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish_with(
                server,
                "r1",
                {"c2[0]": "failed", "c2[1]": "error", "c1[0]": "skipped"},
            )

            status, report = server.json(
                "POST", "/v1/runs/r1/retry", {"id": "r2", "outcomes": ["error"]}
            )
            self.assertEqual(status, 201)
            self.assertEqual(
                [i["instance_id"] for i in report["instances"]], ["c2[1]"]
            )
            self.assertEqual(report["summary"]["total"], 1)
            self.assertEqual(report["summary"]["pending"], 1)

            # Outcome listing order must not reorder the instances.
            status, report = server.json(
                "POST",
                "/v1/runs/r1/retry",
                {"id": "r3", "outcomes": ["error", "failed"]},
            )
            self.assertEqual(status, 201)
            self.assertEqual(
                [i["instance_id"] for i in report["instances"]],
                ["c2[0]", "c2[1]"],
            )

            status, report = server.json(
                "POST",
                "/v1/runs/r1/retry",
                {"id": "r4", "outcomes": ["failed"]},
            )
            self.assertEqual(status, 201)
            self.assertEqual(
                [i["instance_id"] for i in report["instances"]], ["c2[0]"]
            )

    def test_retry_chain_attempt_and_root(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            finish_with(server, "r1", {"c2[0]": "failed", "c2[1]": "error"})

            status, r2 = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            self.assertEqual(r2["attempt"], 1)
            self.assertEqual(r2["retry_of"], "r1")
            self.assertEqual(r2["root_run_id"], "r1")

            # Only one instance fails on the second attempt.
            finish_with(server, "r2", {"c2[0]": "passed", "c2[1]": "failed"})

            status, r3 = server.json("POST", "/v1/runs/r2/retry", {"id": "r3"})
            self.assertEqual(status, 201)
            self.assertEqual(r3["attempt"], 2)
            self.assertEqual(r3["retry_of"], "r2")
            self.assertEqual(r3["root_run_id"], "r1")
            self.assertEqual(
                [i["instance_id"] for i in r3["instances"]], ["c2[1]"]
            )

            finish_with(server, "r3", {"c2[1]": "error"})
            status, r4 = server.json("POST", "/v1/runs/r3/retry", {"id": "r4"})
            self.assertEqual(status, 201)
            self.assertEqual(r4["attempt"], 3)
            self.assertEqual(r4["retry_of"], "r3")
            self.assertEqual(r4["root_run_id"], "r1")
            self.assertEqual(
                [i["instance_id"] for i in r4["instances"]], ["c2[1]"]
            )

    def test_retry_filters_by_direct_source_only(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed"})

            server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            finish_with(server, "r2", {"c1[0]": "passed"})

            # The direct source r2 ended green, even though root r1 failed.
            status, body = server.json("POST", "/v1/runs/r2/retry", {"id": "r3"})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "retry_not_needed")
            status, body = server.json(
                "POST",
                "/v1/runs/r2/retry",
                {"id": "r3", "outcomes": ["failed", "error"]},
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "retry_not_needed")
            # The failed id was not consumed.
            status, _ = server.json("GET", "/v1/runs/r3")
            self.assertEqual(status, 404)

    def test_branches_from_same_source_are_independent(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed"})

            status, a = server.json("POST", "/v1/runs/r1/retry", {"id": "rA"})
            self.assertEqual(status, 201)
            status, b = server.json("POST", "/v1/runs/r1/retry", {"id": "rB"})
            self.assertEqual(status, 201)
            self.assertEqual(a["instances"], b["instances"])

            submit(server, "rA", "c1[0]", "passed")
            status, report_a = server.json("POST", "/v1/runs/rA/complete")
            self.assertEqual(status, 200)
            self.assertEqual(report_a["passed"], True)

            # Branch B is untouched by branch A.
            status, report_b = server.json("GET", "/v1/runs/rB")
            self.assertEqual(status, 200)
            self.assertEqual(report_b["status"], "open")
            self.assertEqual(report_b["instances"][0]["outcome"], "pending")

            # Retrying a branch keeps the original root.
            finish_with(server, "rB", {"c1[0]": "failed"})
            status, c = server.json("POST", "/v1/runs/rB/retry", {"id": "rC"})
            self.assertEqual(status, 201)
            self.assertEqual(c["retry_of"], "rB")
            self.assertEqual(c["root_run_id"], "r1")
            self.assertEqual(c["attempt"], 2)

    def test_retry_uses_existing_result_and_completion_semantics(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            finish_with(server, "r1", {"c2[0]": "passed", "c2[1]": "failed"})

            status, retry = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)

            status, body = server.json("POST", "/v1/runs/r2/complete")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            status, updated = submit(
                server, "r2", "c2[1]", "failed", duration_ms=9
            )
            self.assertEqual(status, 200)
            self.assertEqual(updated["summary"]["failed"], 1)
            self.assertEqual(updated["summary"]["duration_ms"], 9)

            status, body = submit(server, "r2", "c2[1]", "failed")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "result_exists")

            status, completed = server.json("POST", "/v1/runs/r2/complete")
            self.assertEqual(status, 200)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["passed"], False)
            self.assertEqual(completed["attempt"], 1)
            self.assertEqual(completed["root_run_id"], "r1")

            status, body = submit(server, "r2", "c2[1]", "failed")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_completed")

    def test_error_codes_and_precedence(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})

            server.json("POST", "/v1/runs", {"id": "open", "case_ids": ["c1"]})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "failed"})

            # Unknown source run wins over every other 409 check.
            status, body = server.json(
                "POST", "/v1/runs/ghost/retry", {"id": "r1"}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            # Malformed request bodies still fail as 400 before routing state.
            status, body = server.json(
                "POST",
                "/v1/runs/ghost/retry",
                {"id": "r1", "outcomes": ["nope"]},
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            # Incomplete source beats retry_not_needed and run_exists.
            status, body = server.json(
                "POST", "/v1/runs/open/retry", {"id": "r1"}
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # An all-green completed run has nothing to retry.
            status, body = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "retry_not_needed")

            # A retryable source with a duplicated new id collides last.
            status, body = server.json("POST", "/v1/runs/r2/retry", {"id": "r1"})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_exists")

    def test_validation_errors(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed"})

            bad_payloads = [
                ["r2"],
                "r2",
                123,
                None,
                {},
                {"outcomes": ["failed"]},
                {"id": "   ", "outcomes": ["failed"]},
                {"id": "r2", "outcomes": ["failed"], "extra": 1},
                {"id": "r2", "outcomes": []},
                {"id": "r2", "outcomes": None},
                {"id": "r2", "outcomes": ["failed", "failed"]},
                {"id": "r2", "outcomes": ["passed"]},
                {"id": "r2", "outcomes": ["failed", "error", "error"]},
                {"id": "r2", "outcomes": ["failed", 1]},
                {"id": "r2", "outcomes": "failed"},
                {"id": "r2", "outcomes": {}},
            ]
            for payload in bad_payloads:
                status, body = server.json("POST", "/v1/runs/r1/retry", payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(body["error"]["code"], "validation_error", payload)
                status, check = server.json("GET", "/v1/runs/r2")
                self.assertEqual(status, 404, payload)

            # Malformed JSON is invalid_json, and still consumes no id.
            status, body = server.json(
                "POST", "/v1/runs/r1/retry", raw_body=b"{not json"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")
            status, _ = server.json("GET", "/v1/runs/r2")
            self.assertEqual(status, 404)

            # A failed validation must not burn the new id.
            status, report = server.json(
                "POST", "/v1/runs/r1/retry", {"id": "r2", "outcomes": ["bogus"]}
            )
            self.assertEqual(status, 400)
            status, report = server.json(
                "POST", "/v1/runs/r1/retry", {"id": "r2"}
            )
            self.assertEqual(status, 201)
            self.assertEqual(report["id"], "r2")

    def test_restart_clears_retries(self) -> None:
        service = service_module.Service()
        with ServerHarness(service) as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed"})
            status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)

        with ServerHarness(service_module.Service()) as server:
            for run_id in ("r1", "r2"):
                status, body = server.json("GET", f"/v1/runs/{run_id}")
                self.assertEqual(status, 404)
                self.assertEqual(body["error"]["code"], "run_not_found")


if __name__ == "__main__":
    unittest.main()
