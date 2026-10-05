"""End-to-end HTTP tests for the read-only CI gate endpoint."""

from __future__ import annotations

import math
import unittest

import testlattice.service as service_module
from test_retries import finish_with
from test_runs import ServerHarness, seed_cases


def evaluate(server: ServerHarness, run_ids: list[str], policy: dict) -> tuple[int, dict]:
    return server.json(
        "POST", "/v1/ci/gates/evaluate", {"run_ids": run_ids, "policy": policy}
    )


def submit(
    server: ServerHarness,
    run_id: str,
    instance_id: str,
    outcome: str = "passed",
    coverage: object = None,
) -> tuple[int, dict]:
    payload: dict = {
        "instance_id": instance_id,
        "outcome": outcome,
        "duration_ms": 5,
    }
    if coverage is not None:
        payload["coverage"] = coverage
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def complete(server: ServerHarness, run_id: str, results: dict[str, str]) -> None:
    for instance_id, outcome in results.items():
        status, _ = submit(server, run_id, instance_id, outcome)
        assert status == 200, (instance_id, outcome, status)
    status, _ = server.json("POST", f"/v1/runs/{run_id}/complete")
    assert status == 200


COVERAGE_TWO_THIRDS = {
    "files": {"a.py": {"executable_lines": [1, 2, 3], "covered_lines": [1, 2]}}
}
COVERAGE_FULL = {
    "files": {"a.py": {"executable_lines": [1, 2, 3], "covered_lines": [1, 2, 3]}}
}


class CiGateTest(unittest.TestCase):
    def test_checks_use_current_run_counts_and_fixed_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            complete(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "error"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1", "c2"]})
            complete(
                server,
                "r2",
                {"c1[0]": "passed", "c2[0]": "passed", "c2[1]": "passed"},
            )

            status, report = evaluate(
                server,
                ["r1", "r2"],
                {"max_failed": 0, "max_error": 0},
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["current_run_id"], "r2")
            self.assertEqual(report["run_count"], 2)
            self.assertIs(report["passed"], True)
            self.assertEqual(
                [c["name"] for c in report["checks"]],
                ["max_failed", "max_error"],
            )
            self.assertEqual(
                report["checks"][0],
                {"name": "max_failed", "passed": True, "actual": 0, "limit": 0},
            )
            self.assertEqual(
                report["checks"][1],
                {"name": "max_error", "passed": True, "actual": 0, "limit": 0},
            )

            # The current run is the last run id even when it is the failing one.
            status, report = evaluate(
                server, ["r2", "r1"], {"max_failed": 0, "max_error": 0}
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["current_run_id"], "r1")
            self.assertIs(report["passed"], False)
            self.assertEqual(report["checks"][0]["actual"], 1)
            self.assertIs(report["checks"][0]["passed"], False)
            self.assertEqual(report["checks"][1]["actual"], 1)

            # Only requested checks appear.
            status, report = evaluate(server, ["r1"], {"max_error": 1})
            self.assertEqual(status, 200)
            self.assertEqual([c["name"] for c in report["checks"]], ["max_error"])
            self.assertIs(report["passed"], True)
            status, report = evaluate(server, ["r1"], {"max_error": 0})
            self.assertIs(report["passed"], False)

    def test_top_level_passed_requires_every_check(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            complete(server, "r1", {"c1[0]": "failed"})
            status, report = evaluate(
                server, ["r1"], {"max_failed": 0, "max_error": 0}
            )
            self.assertEqual(status, 200)
            self.assertIs(report["passed"], False)
            self.assertEqual(
                [c["passed"] for c in report["checks"]], [False, True]
            )

    def test_coverage_check_uses_merged_current_run_percent(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, _ = submit(
                server, "r1", "c1[0]", "passed", coverage=COVERAGE_TWO_THIRDS
            )
            self.assertEqual(status, 200)
            status, _ = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 200)

            status, report = evaluate(server, ["r1"], {"min_coverage_percent": 66.67})
            self.assertEqual(status, 200)
            check = report["checks"][0]
            self.assertEqual(check["name"], "min_coverage_percent")
            self.assertEqual(check["actual"], 66.67)
            self.assertEqual(check["limit"], 66.67)
            self.assertIs(check["passed"], True)
            self.assertIs(report["passed"], True)

            # actual >= limit is required; just above the rounded percent fails.
            status, report = evaluate(server, ["r1"], {"min_coverage_percent": 66.68})
            self.assertIs(report["checks"][0]["passed"], False)
            self.assertIs(report["passed"], False)

            # Full coverage against the maximum threshold passes.
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            status, _ = submit(
                server, "r2", "c1[0]", "passed", coverage=COVERAGE_FULL
            )
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/r2/complete")
            status, report = evaluate(server, ["r2"], {"min_coverage_percent": 100})
            self.assertEqual(report["checks"][0]["actual"], 100.0)
            self.assertIs(report["passed"], True)

    def test_coverage_without_executable_lines_is_null_and_fails(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            complete(server, "r1", {"c1[0]": "passed"})

            # Even a zero threshold cannot pass when coverage is undefined.
            status, report = evaluate(server, ["r1"], {"min_coverage_percent": 0})
            self.assertEqual(status, 200)
            check = report["checks"][0]
            self.assertIsNone(check["actual"])
            self.assertEqual(check["limit"], 0)
            self.assertIs(check["passed"], False)
            self.assertIs(report["passed"], False)

    def test_flaky_uses_case_and_instance_pair_and_ignores_skipped(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            complete(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "passed"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1", "c2"]})
            complete(
                server,
                "r2",
                # c1[0] flips pass -> fail (flaky); c2[1] failed -> skipped,
                # so it only has one effective observation.
                {"c1[0]": "failed", "c2[0]": "passed", "c2[1]": "skipped"},
            )

            status, report = evaluate(
                server, ["r1", "r2"], {"max_flaky": 1}
            )
            self.assertEqual(status, 200)
            check = report["checks"][0]
            self.assertEqual(check["name"], "max_flaky")
            self.assertEqual(check["actual"], 1)
            self.assertEqual(check["limit"], 1)
            self.assertIs(check["passed"], True)
            self.assertIs(report["passed"], True)

            status, report = evaluate(server, ["r1", "r2"], {"max_flaky": 0})
            self.assertEqual(report["checks"][0]["actual"], 1)
            self.assertIs(report["passed"], False)

    def test_error_together_with_passed_is_flaky(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            complete(server, "r1", {"c1[0]": "error"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            complete(server, "r2", {"c1[0]": "passed"})
            status, report = evaluate(server, ["r1", "r2"], {"max_flaky": 0})
            self.assertEqual(status, 200)
            self.assertEqual(report["checks"][0]["actual"], 1)
            self.assertIs(report["passed"], False)

    def test_single_observation_or_skipped_only_is_not_flaky(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            # c2 only appears in r1; c1 is skipped in both runs.
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            complete(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "passed", "c1[0]": "skipped"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            complete(server, "r2", {"c1[0]": "skipped"})

            status, report = evaluate(server, ["r1", "r2"], {"max_flaky": 0})
            self.assertEqual(status, 200)
            self.assertEqual(report["checks"][0]["actual"], 0)
            self.assertIs(report["passed"], True)

    def test_failed_then_error_without_pass_is_not_flaky(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            complete(server, "r1", {"c1[0]": "failed"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            complete(server, "r2", {"c1[0]": "error"})
            status, report = evaluate(server, ["r1", "r2"], {"max_flaky": 0})
            self.assertEqual(status, 200)
            self.assertEqual(report["checks"][0]["actual"], 0)

    def test_all_four_checks_keep_declared_output_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            complete(server, "r1", {"c1[0]": "failed"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            status, _ = submit(
                server, "r2", "c1[0]", "passed", coverage=COVERAGE_FULL
            )
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/r2/complete")

            # Declaration order in the request is ignored; the output order is
            # always max_failed, max_error, min_coverage_percent, max_flaky.
            policy = {
                "max_flaky": 0,
                "min_coverage_percent": 100,
                "max_error": 0,
                "max_failed": 0,
            }
            status, report = evaluate(server, ["r1", "r2"], policy)
            self.assertEqual(status, 200)
            self.assertEqual(
                [c["name"] for c in report["checks"]],
                ["max_failed", "max_error", "min_coverage_percent", "max_flaky"],
            )
            self.assertEqual(
                set(report), {"current_run_id", "run_count", "passed", "checks"}
            )
            # r2 is current: no failures/errors, full coverage, c1 flipped
            # failed -> passed so the one flaky instance fails max_flaky=0.
            self.assertEqual(
                [c["passed"] for c in report["checks"]],
                [True, True, True, False],
            )
            self.assertIs(report["passed"], False)

    def test_retry_runs_participate_like_normal_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed"})
            status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            finish_with(server, "r2", {"c1[0]": "passed"})

            status, report = evaluate(
                server,
                ["r1", "r2"],
                {"max_failed": 0, "max_flaky": 1},
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["current_run_id"], "r2")
            self.assertEqual(report["checks"][0]["actual"], 0)
            self.assertEqual(report["checks"][1]["actual"], 1)
            self.assertIs(report["passed"], True)

    def test_archived_runs_participate_like_normal_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed"})
            status, archive = server.json("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)

        with ServerHarness(service_module.Service()) as restored:
            status, _ = restored.json("POST", "/v1/run-archives", archive)
            self.assertEqual(status, 201)
            status, report = evaluate(restored, ["r1"], {"max_failed": 1})
            self.assertEqual(status, 200)
            self.assertEqual(report["current_run_id"], "r1")
            self.assertEqual(report["run_count"], 1)
            self.assertIs(report["passed"], True)
            status, report = evaluate(restored, ["r1"], {"max_failed": 0})
            self.assertIs(report["passed"], False)

    def test_validation_errors(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            complete(server, "r1", {"c1[0]": "passed"})

            bad_payloads = [
                [],
                "x",
                123,
                None,
                True,
                {},
                {"run_ids": ["r1"]},
                {"policy": {"max_failed": 0}},
                {"run_ids": ["r1"], "policy": {}, "extra": 1},
                {"run_ids": ["r1"], "policy": {"max_failed": 0}, "extra": 1},
                {"run_ids": "r1", "policy": {"max_failed": 0}},
                {"run_ids": None, "policy": {"max_failed": 0}},
                {"run_ids": [], "policy": {"max_failed": 0}},
                {"run_ids": [f"r{i}" for i in range(101)], "policy": {"max_failed": 0}},
                {"run_ids": ["r1", "r1"], "policy": {"max_failed": 0}},
                {"run_ids": ["", "r1"], "policy": {"max_failed": 0}},
                {"run_ids": [1, "r1"], "policy": {"max_failed": 0}},
                {"run_ids": [None], "policy": {"max_failed": 0}},
                {"run_ids": [["r1"]], "policy": {"max_failed": 0}},
                {"run_ids": ["r1"], "policy": []},
                {"run_ids": ["r1"], "policy": None},
                {"run_ids": ["r1"], "policy": "max_failed"},
                {"run_ids": ["r1"], "policy": {}},
                {"run_ids": ["r1"], "policy": {"unknown": 1}},
                {"run_ids": ["r1"], "policy": {"max_failed": -1}},
                {"run_ids": ["r1"], "policy": {"max_failed": 1.5}},
                {"run_ids": ["r1"], "policy": {"max_failed": True}},
                {"run_ids": ["r1"], "policy": {"max_failed": "0"}},
                {"run_ids": ["r1"], "policy": {"max_failed": None}},
                {"run_ids": ["r1"], "policy": {"max_error": -1}},
                {"run_ids": ["r1"], "policy": {"max_error": 0.5}},
                {"run_ids": ["r1"], "policy": {"max_error": False}},
                {"run_ids": ["r1"], "policy": {"max_flaky": -1}},
                {"run_ids": ["r1"], "policy": {"max_flaky": 1.0}},
                # Flakiness needs at least two runs to be meaningful.
                {"run_ids": ["r1"], "policy": {"max_flaky": 0}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": -0.01}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": 100.01}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": True}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": False}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": "80"}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": None}},
                {"run_ids": ["r1"], "policy": {"min_coverage_percent": [1]}},
            ]
            for payload in bad_payloads:
                status, body = server.json(
                    "POST", "/v1/ci/gates/evaluate", payload
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(
                    body["error"]["code"], "validation_error", payload
                )

            for non_finite in (math.nan, math.inf, -math.inf):
                status, body = server.json(
                    "POST",
                    "/v1/ci/gates/evaluate",
                    {"run_ids": ["r1"], "policy": {"min_coverage_percent": non_finite}},
                )
                self.assertEqual(status, 400, non_finite)
                self.assertEqual(body["error"]["code"], "validation_error")

            status, body = server.json(
                "POST", "/v1/ci/gates/evaluate", raw_body=b"{not json"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

    def test_boundary_run_counts_and_flaky_requirement(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            complete(server, "r1", {"c1[0]": "passed"})

            # Exactly 100 unique runs with max_flaky is acceptable.
            run_ids = ["r1"]
            for index in range(99):
                run_id = f"b{index}"
                server.json("POST", "/v1/runs", {"id": run_id, "case_ids": ["c1"]})
                complete(server, run_id, {"c1[0]": "passed"})
                run_ids.append(run_id)
            status, report = evaluate(server, run_ids, {"max_flaky": 0})
            self.assertEqual(status, 200)
            self.assertEqual(report["run_count"], 100)
            self.assertEqual(report["current_run_id"], "b98")
            self.assertEqual(report["checks"][0]["actual"], 0)

            # Two runs are enough to lift the max_flaky requirement.
            status, report = evaluate(
                server, ["r1", "b0"], {"max_flaky": 0}
            )
            self.assertEqual(status, 200)

    def test_missing_and_incomplete_runs_checked_in_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "done", "case_ids": ["c1"]})
            complete(server, "done", {"c1[0]": "passed"})
            server.json("POST", "/v1/runs", {"id": "open", "case_ids": ["c1"]})

            status, body = evaluate(server, ["done", "ghost"], {"max_failed": 0})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            status, body = evaluate(server, ["done", "open"], {"max_failed": 0})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # The first offending run in request order decides the error.
            status, body = evaluate(server, ["ghost", "open"], {"max_failed": 0})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")
            status, body = evaluate(server, ["open", "ghost"], {"max_failed": 0})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # Structural problems still win over run lookups.
            status, body = server.json(
                "POST",
                "/v1/ci/gates/evaluate",
                {"run_ids": ["ghost"], "policy": {}},
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

    def test_read_only_deterministic_and_isolated(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, _ = submit(
                server, "r1", "c1[0]", "failed", coverage=COVERAGE_TWO_THIRDS
            )
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/r1/complete")
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            complete(server, "r2", {"c1[0]": "passed"})

            _, before_r1 = server.json("GET", "/v1/runs/r1")
            _, before_r2 = server.json("GET", "/v1/runs/r2")
            _, before_cov = server.json("GET", "/v1/runs/r1/coverage")

            policy = {
                "max_failed": 0,
                "max_error": 0,
                "min_coverage_percent": 50,
                "max_flaky": 0,
            }
            status, first = evaluate(server, ["r1", "r2"], policy)
            self.assertEqual(status, 200)
            status, second = evaluate(server, ["r1", "r2"], policy)
            self.assertEqual(status, 200)
            self.assertEqual(first, second)

            _, after_r1 = server.json("GET", "/v1/runs/r1")
            _, after_r2 = server.json("GET", "/v1/runs/r2")
            _, after_cov = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(before_r1, after_r1)
            self.assertEqual(before_r2, after_r2)
            self.assertEqual(before_cov, after_cov)

    def test_unknown_route_still_not_found(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST",
                "/v1/ci/gates/evaluate/extra",
                {"run_ids": ["r1"], "policy": {"max_failed": 0}},
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")
            status, body = server.json("GET", "/v1/ci/gates/evaluate")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
