"""End-to-end HTTP tests for the read-only CI gate evaluation entry."""

from __future__ import annotations

import json
import unittest

import testlattice.service as service_module
from test_runs import ServerHarness, seed_cases

PATH = "/v1/ci/gates/evaluate"


def evaluate(server: ServerHarness, payload: object) -> tuple[int, dict]:
    return server.json("POST", PATH, payload)


def submit(
    server: ServerHarness,
    run_id: str,
    instance_id: str,
    outcome: str = "passed",
    duration_ms: int = 5,
    **extra: object,
) -> tuple[int, dict]:
    payload = {
        "instance_id": instance_id,
        "outcome": outcome,
        "duration_ms": duration_ms,
    }
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def complete(server: ServerHarness, run_id: str) -> None:
    status, _ = server.json("POST", f"/v1/runs/{run_id}/complete")
    assert status == 200, (run_id, status)


def finish(
    server: ServerHarness, run_id: str, results: dict[str, str | tuple[str, object]]
) -> None:
    """Submit per-instance outcomes (optionally with coverage) and complete."""
    for instance_id, spec in results.items():
        if isinstance(spec, tuple):
            outcome, coverage = spec
            status, _ = submit(
                server, run_id, instance_id, outcome, coverage=coverage
            )
        else:
            status, _ = submit(server, run_id, instance_id, spec)
        assert status == 200, (run_id, instance_id, spec, status)
    complete(server, run_id)


TWO_THIRDS_COVERAGE = {
    "files": {"f.py": {"executable_lines": [1, 2, 3], "covered_lines": [1, 2]}}
}


class CiGateTest(unittest.TestCase):
    def test_success_envelope_uses_last_run_as_current(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": "passed"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish(server, "r2", {"c1[0]": "passed"})

            status, report = evaluate(
                server, {"run_ids": ["r1", "r2"], "policy": {"max_failed": 0}}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                set(report), {"current_run_id", "run_count", "passed", "checks"}
            )
            self.assertEqual(report["current_run_id"], "r2")
            self.assertEqual(report["run_count"], 2)
            self.assertTrue(report["passed"])
            self.assertEqual(
                report["checks"],
                [{"name": "max_failed", "passed": True, "actual": 0, "limit": 0}],
            )

    def test_checks_only_include_requested_policies_in_fixed_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": "passed"})

            status, report = evaluate(
                server, {"run_ids": ["r1"], "policy": {"max_error": 3}}
            )
            self.assertEqual(status, 200)
            self.assertEqual([c["name"] for c in report["checks"]], ["max_error"])

            # Declared out of order; emitted in canonical order.
            status, report = evaluate(
                server,
                {
                    "run_ids": ["r1"],
                    "policy": {
                        "max_flaky": 0,
                        "min_coverage_percent": 0,
                        "max_failed": 1,
                    },
                },
            )
            self.assertEqual(status, 400)  # max_flaky needs two runs

            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish(server, "r2", {"c1[0]": "passed"})
            status, report = evaluate(
                server,
                {
                    "run_ids": ["r1", "r2"],
                    "policy": {
                        "max_flaky": 0,
                        "min_coverage_percent": 0,
                        "max_failed": 1,
                        "max_error": 2,
                    },
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                [c["name"] for c in report["checks"]],
                ["max_failed", "max_error", "min_coverage_percent", "max_flaky"],
            )

    def test_failed_and_error_come_from_current_run_only(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            # Old run: two failures, no errors.
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            finish(server, "r1", {"c2[0]": "failed", "c2[1]": "failed"})
            # Current run: one failure and one error.
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c2"]})
            finish(server, "r2", {"c2[0]": "failed", "c2[1]": "error"})

            status, report = evaluate(
                server,
                {
                    "run_ids": ["r1", "r2"],
                    "policy": {"max_failed": 1, "max_error": 1},
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["checks"][0]["actual"], 1)
            self.assertEqual(report["checks"][1]["actual"], 1)
            self.assertTrue(report["passed"])

            # The old run's two failures must not leak into the current run.
            status, report = evaluate(
                server,
                {"run_ids": ["r1", "r2"], "policy": {"max_failed": 0, "max_error": 0}},
            )
            self.assertEqual(status, 200)
            self.assertFalse(report["passed"])
            failed_check, error_check = report["checks"]
            self.assertFalse(failed_check["passed"])
            self.assertFalse(error_check["passed"])

            # Pointing at the old run as current uses its final results.
            status, report = evaluate(
                server, {"run_ids": ["r1"], "policy": {"max_failed": 2}}
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["current_run_id"], "r1")
            self.assertTrue(report["passed"])

    def test_min_coverage_percent_uses_merged_current_run_total(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            # Old run covers 1/1 lines; that coverage must not merge forward.
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(
                server,
                "r1",
                {"c1[0]": ("passed", {"files": {"old.py": {"executable_lines": [1], "covered_lines": [1]}}})},
            )
            # Current run covers 2/3 -> 66.67 percent.
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c2"]})
            finish(
                server,
                "r2",
                {
                    "c2[0]": ("passed", TWO_THIRDS_COVERAGE),
                    "c2[1]": "failed",
                },
            )

            status, report = evaluate(
                server,
                {"run_ids": ["r1", "r2"], "policy": {"min_coverage_percent": 66}},
            )
            self.assertEqual(status, 200)
            check = report["checks"][0]
            self.assertEqual(check["name"], "min_coverage_percent")
            self.assertEqual(check["actual"], 66.67)
            self.assertEqual(check["limit"], 66)
            self.assertTrue(check["passed"])
            self.assertTrue(report["passed"])

            status, report = evaluate(
                server,
                {"run_ids": ["r1", "r2"], "policy": {"min_coverage_percent": 66.67}},
            )
            self.assertEqual(status, 200)
            self.assertTrue(report["checks"][0]["passed"])

            status, report = evaluate(
                server,
                {"run_ids": ["r1", "r2"], "policy": {"min_coverage_percent": 67}},
            )
            self.assertEqual(status, 200)
            self.assertFalse(report["checks"][0]["passed"])
            self.assertFalse(report["passed"])

    def test_coverage_without_executable_lines_is_null_and_fails(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": "passed"})

            # Even a limit of zero fails when there are no executable lines.
            status, report = evaluate(
                server,
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": 0}},
            )
            self.assertEqual(status, 200)
            check = report["checks"][0]
            self.assertIsNone(check["actual"])
            self.assertEqual(check["limit"], 0)
            self.assertFalse(check["passed"])
            self.assertFalse(report["passed"])

    def test_flaky_instances_keyed_by_case_and_instance(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish(
                server,
                "r1",
                {"c2[0]": "failed", "c2[1]": "skipped", "c1[0]": "passed"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c2", "c1"]})
            finish(
                server,
                "r2",
                {"c2[0]": "passed", "c2[1]": "passed", "c1[0]": "failed"},
            )
            # r3 only contains c1; the c2 instances gain no observation here.
            server.json("POST", "/v1/runs", {"id": "r3", "case_ids": ["c1"]})
            finish(server, "r3", {"c1[0]": "passed"})

            # c2[0]: failed then passed -> flaky.
            # c2[1]: skipped (ignored) then one passed -> not flaky.
            # c1[0]: passed, failed, passed -> flaky.
            status, report = evaluate(
                server,
                {"run_ids": ["r1", "r2", "r3"], "policy": {"max_flaky": 2}},
            )
            self.assertEqual(status, 200)
            check = report["checks"][0]
            self.assertEqual(check["actual"], 2)
            self.assertEqual(check["limit"], 2)
            self.assertTrue(check["passed"])
            self.assertTrue(report["passed"])

            status, report = evaluate(
                server,
                {"run_ids": ["r1", "r2", "r3"], "policy": {"max_flaky": 1}},
            )
            self.assertEqual(status, 200)
            self.assertFalse(report["checks"][0]["passed"])
            self.assertFalse(report["passed"])

    def test_error_observation_makes_an_instance_flaky(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": "error"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish(server, "r2", {"c1[0]": "passed"})

            status, report = evaluate(
                server, {"run_ids": ["r1", "r2"], "policy": {"max_flaky": 0}}
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["checks"][0]["actual"], 1)
            self.assertFalse(report["passed"])

    def test_skipped_only_and_single_observations_are_not_flaky(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "skipped", "c1[0]": "skipped"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c2"]})
            finish(server, "r2", {"c2[0]": "passed", "c2[1]": "skipped"})

            status, report = evaluate(
                server, {"run_ids": ["r1", "r2"], "policy": {"max_flaky": 0}}
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["checks"][0]["actual"], 0)
            self.assertTrue(report["passed"])

    def test_top_level_passed_requires_every_check(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            finish(
                server,
                "r1",
                {"c2[0]": ("passed", TWO_THIRDS_COVERAGE), "c2[1]": "failed"},
            )
            status, report = evaluate(
                server,
                {
                    "run_ids": ["r1"],
                    "policy": {"max_failed": 1, "min_coverage_percent": 90},
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(report["checks"][0]["passed"])
            self.assertFalse(report["checks"][1]["passed"])
            self.assertFalse(report["passed"])

    def test_retry_runs_participate_like_normal_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": "failed"})
            status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            finish(server, "r2", {"c1[0]": "passed"})

            status, report = evaluate(
                server, {"run_ids": ["r1", "r2"], "policy": {"max_flaky": 0}}
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["current_run_id"], "r2")
            self.assertEqual(report["checks"][0]["actual"], 1)
            self.assertFalse(report["passed"])

            # The retry run's own final result drives the failed/error check.
            status, report = evaluate(
                server, {"run_ids": ["r1", "r2"], "policy": {"max_failed": 0}}
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["checks"][0]["actual"], 0)
            self.assertTrue(report["passed"])

    def test_archive_restored_runs_participate_without_catalog(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": ("passed", TWO_THIRDS_COVERAGE)})
            status, _, raw = server.request("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)

        with ServerHarness(service_module.Service()) as restored:
            status, _ = restored.json("POST", "/v1/run-archives", raw_body=raw)
            self.assertEqual(status, 201)
            # No catalog exists in this process, yet the gate still works.
            status, report = evaluate(
                restored,
                {
                    "run_ids": ["r1"],
                    "policy": {"max_failed": 0, "min_coverage_percent": 66.67},
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(report["passed"])
            self.assertEqual(
                report["checks"][1]["actual"], 66.67
            )

    def test_missing_and_incomplete_runs_checked_in_request_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "done", "case_ids": ["c1"]})
            finish(server, "done", {"c1[0]": "passed"})
            server.json("POST", "/v1/runs", {"id": "open", "case_ids": ["c1"]})
            policy = {"max_failed": 0}

            status, body = evaluate(
                server, {"run_ids": ["done", "ghost"], "policy": policy}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            status, body = evaluate(
                server, {"run_ids": ["done", "open"], "policy": policy}
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # The first offending run decides the error; validation of policy
            # (structure) still happens before either run lookup.
            status, body = evaluate(
                server, {"run_ids": ["ghost", "open"], "policy": policy}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")
            status, body = evaluate(
                server, {"run_ids": ["open", "ghost"], "policy": policy}
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

    def test_validation_errors(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": "passed"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish(server, "r2", {"c1[0]": "passed"})

            bad_payloads = [
                [],
                "x",
                123,
                None,
                {},
                {"policy": {"max_failed": 0}},
                {"run_ids": ["r1"]},
                {"run_ids": ["r1"], "policy": {}, "extra": 1},
                {"run_ids": ["r1"], "policy": {"max_failed": 0}, "extra": 1},
                {"run_ids": "r1", "policy": {"max_failed": 0}},
                {"run_ids": None, "policy": {"max_failed": 0}},
                {"run_ids": [], "policy": {"max_failed": 0}},
                {"run_ids": ["r1", "r1"], "policy": {"max_failed": 0}},
                {"run_ids": ["", "r1"], "policy": {"max_failed": 0}},
                {"run_ids": [1, "r1"], "policy": {"max_failed": 0}},
                {"run_ids": [None], "policy": {"max_failed": 0}},
                {"run_ids": [f"r{i}" for i in range(101)], "policy": {"max_failed": 0}},
                {"run_ids": ["r1"], "policy": None},
                {"run_ids": ["r1"], "policy": []},
                {"run_ids": ["r1"], "policy": {}},
                {"run_ids": ["r1"], "policy": {"unknown": 1}},
                {"run_ids": ["r1"], "policy": {"max_failed": True}},
                {"run_ids": ["r1"], "policy": {"max_failed": False}},
                {"run_ids": ["r1"], "policy": {"max_failed": -1}},
                {"run_ids": ["r1"], "policy": {"max_failed": 1.0}},
                {"run_ids": ["r1"], "policy": {"max_failed": "0"}},
                {"run_ids": ["r1"], "policy": {"max_error": -1}},
                {"run_ids": ["r1"], "policy": {"max_flaky": -1}},
                {"run_ids": ["r1"], "policy": {"max_flaky": 1.5}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": True}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": "80"}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": -0.1}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": 100.1}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": None}},
            ]
            for payload in bad_payloads:
                status, body = evaluate(server, payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(
                    body["error"]["code"], "validation_error", payload
                )

            # max_flaky needs at least two runs even if those runs are missing;
            # structural validation precedes run lookup.
            status, body = evaluate(
                server, {"run_ids": ["ghost"], "policy": {"max_flaky": 0}}
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            # Non-finite coverage limits are invalid JSON numbers.
            for token in ("NaN", "Infinity", "-Infinity"):
                status, body = server.json(
                    "POST",
                    PATH,
                    raw_body=(
                        b'{"run_ids": ["r1"], "policy": '
                        b'{"min_coverage_percent": ' + token.encode() + b"}}"
                    ),
                )
                self.assertEqual(status, 400, token)
                self.assertEqual(body["error"]["code"], "validation_error", token)

            # Malformed JSON maps to invalid_json, not validation_error.
            status, body = server.json(
                "POST", PATH, raw_body=b"{not json"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

    def test_single_run_without_flaky_policy_is_allowed(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": "passed"})
            status, report = evaluate(
                server,
                {"run_ids": ["r1"], "policy": {"max_failed": 0, "max_error": 0}},
            )
            self.assertEqual(status, 200)
            self.assertTrue(report["passed"])

    def test_response_is_stable_and_read_only(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": "failed"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish(
                server,
                "r2",
                {"c1[0]": ("passed", TWO_THIRDS_COVERAGE)},
            )
            _, before_r1 = server.json("GET", "/v1/runs/r1")
            _, before_r2 = server.json("GET", "/v1/runs/r2")
            _, coverage_before = server.json("GET", "/v1/runs/r2/coverage")
            payload = {
                "run_ids": ["r1", "r2"],
                "policy": {
                    "max_flaky": 0,
                    "min_coverage_percent": 50,
                    "max_failed": 0,
                    "max_error": 0,
                },
            }

            status, first = evaluate(server, json.loads(json.dumps(payload)))
            self.assertEqual(status, 200)
            status, second = evaluate(server, json.loads(json.dumps(payload)))
            self.assertEqual(status, 200)
            self.assertEqual(first, second)

            _, after_r1 = server.json("GET", "/v1/runs/r1")
            _, after_r2 = server.json("GET", "/v1/runs/r2")
            _, coverage_after = server.json("GET", "/v1/runs/r2/coverage")
            self.assertEqual(before_r1, after_r1)
            self.assertEqual(before_r2, after_r2)
            self.assertEqual(coverage_before, coverage_after)
            _, cases = server.json("GET", "/v1/cases")
            self.assertEqual(len(cases), 3)

    def test_restart_clears_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": "passed"})
            status, _ = evaluate(
                server, {"run_ids": ["r1"], "policy": {"max_failed": 0}}
            )
            self.assertEqual(status, 200)

        with ServerHarness(service_module.Service()) as server:
            status, body = evaluate(
                server, {"run_ids": ["r1"], "policy": {"max_failed": 0}}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

    def test_unknown_route_still_not_found(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST",
                "/v1/ci/gates/evaluate/extra",
                {"run_ids": [], "policy": {"max_failed": 0}},
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")

            status, body = server.json(
                "GET", "/v1/ci/gates/evaluate"
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")

            status, body = server.json(
                "POST",
                "/v1/ci/gates",
                {"run_ids": [], "policy": {"max_failed": 0}},
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
