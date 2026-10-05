"""End-to-end HTTP tests for per-instance stability analysis."""

from __future__ import annotations

import unittest

import testlattice.service as service_module
from test_retries import finish_with
from test_runs import ServerHarness, case_payload, seed_cases


def stability(server: ServerHarness, run_ids: list[str]) -> tuple[int, dict]:
    return server.json("POST", "/v1/reports/stability", {"run_ids": run_ids})


class StabilityTest(unittest.TestCase):
    def test_instances_grouped_by_first_appearance(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish_with(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "skipped"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1", "c2"]})
            finish_with(
                server,
                "r2",
                {"c1[0]": "passed", "c2[0]": "passed", "c2[1]": "passed"},
            )

            status, report = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            self.assertEqual(report["run_count"], 2)
            self.assertEqual(
                report["summary"],
                {"total": 3, "stable_pass": 1, "stable_fail": 0, "flaky": 1, "insufficient": 1},
            )

            instances = report["instances"]
            # First-appearance order follows the r1 scan, not the r2 scan.
            self.assertEqual(
                [(i["case_id"], i["instance_id"]) for i in instances],
                [("c2", "c2[0]"), ("c2", "c2[1]"), ("c1", "c1[0]")],
            )

            first = instances[0]
            self.assertEqual(first["case_name"], "name-c2")
            self.assertEqual(first["status"], "stable_pass")
            self.assertEqual(first["pass_rate"], 1.0)
            self.assertEqual(
                first["summary"],
                {
                    "total": 2,
                    "effective": 2,
                    "passed": 2,
                    "failed": 0,
                    "error": 0,
                    "skipped": 0,
                    "duration_ms": 10,
                },
            )
            self.assertEqual(
                first["observations"],
                [
                    {"run_id": "r1", "outcome": "passed", "duration_ms": 5},
                    {"run_id": "r2", "outcome": "passed", "duration_ms": 5},
                ],
            )

            second = instances[1]
            self.assertEqual(second["status"], "flaky")
            self.assertEqual(second["pass_rate"], 0.5)
            self.assertEqual(
                second["summary"],
                {
                    "total": 2,
                    "effective": 2,
                    "passed": 1,
                    "failed": 1,
                    "error": 0,
                    "skipped": 0,
                    "duration_ms": 10,
                },
            )
            self.assertEqual(
                [o["outcome"] for o in second["observations"]],
                ["failed", "passed"],
            )

            third = instances[2]
            # One skipped and one passed observation: only one effective.
            self.assertEqual(third["status"], "insufficient")
            self.assertEqual(third["pass_rate"], 1.0)
            self.assertEqual(
                third["summary"],
                {
                    "total": 2,
                    "effective": 1,
                    "passed": 1,
                    "failed": 0,
                    "error": 0,
                    "skipped": 1,
                    "duration_ms": 10,
                },
            )

    def test_absent_run_adds_no_observation(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c2"]})
            finish_with(server, "r2", {"c2[0]": "passed", "c2[1]": "passed"})
            server.json("POST", "/v1/runs", {"id": "r3", "case_ids": ["c1"]})
            finish_with(server, "r3", {"c1[0]": "passed"})

            status, report = stability(server, ["r1", "r2", "r3"])
            self.assertEqual(status, 200)
            self.assertEqual(report["run_count"], 3)
            by_id = {i["instance_id"]: i for i in report["instances"]}
            c1 = by_id["c1[0]"]
            self.assertEqual(c1["status"], "stable_pass")
            self.assertEqual(c1["summary"]["total"], 2)
            self.assertEqual(
                [o["run_id"] for o in c1["observations"]], ["r1", "r3"]
            )
            # c2 instances only appear in r2: a single effective observation.
            self.assertEqual(by_id["c2[0]"]["status"], "insufficient")
            self.assertEqual(by_id["c2[1]"]["status"], "insufficient")

    def test_stable_fail_covers_failed_and_error(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "error"})

            status, report = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            instance = report["instances"][0]
            self.assertEqual(instance["status"], "stable_fail")
            self.assertEqual(instance["pass_rate"], 0.0)
            self.assertEqual(instance["summary"]["failed"], 1)
            self.assertEqual(instance["summary"]["error"], 1)

    def test_all_skipped_has_null_pass_rate(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "skipped"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "skipped"})

            status, report = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            instance = report["instances"][0]
            self.assertEqual(instance["status"], "insufficient")
            self.assertIsNone(instance["pass_rate"])
            self.assertEqual(instance["summary"]["effective"], 0)
            self.assertEqual(instance["summary"]["skipped"], 2)

    def test_pass_rate_rounding(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            outcomes = ["passed", "passed", "failed"]
            for index, outcome in enumerate(outcomes):
                run_id = f"r{index}"
                server.json("POST", "/v1/runs", {"id": run_id, "case_ids": ["c1"]})
                finish_with(server, run_id, {"c1[0]": outcome})

            status, report = stability(server, ["r0", "r1", "r2"])
            self.assertEqual(status, 200)
            instance = report["instances"][0]
            self.assertEqual(instance["status"], "flaky")
            self.assertEqual(instance["pass_rate"], 0.67)

    def test_case_name_comes_from_most_recent_observation(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})

            server.request("DELETE", "/v1/cases/c1")
            server.json("POST", "/v1/cases", case_payload("c1", name="renamed"))
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "passed"})

            status, report = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            self.assertEqual(report["instances"][0]["case_name"], "renamed")

            # Request order decides which observation is the most recent.
            status, report = stability(server, ["r2", "r1"])
            self.assertEqual(status, 200)
            self.assertEqual(report["instances"][0]["case_name"], "name-c1")

    def test_retry_observations_carry_lineage(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed"})
            server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            finish_with(server, "r2", {"c1[0]": "passed"})

            status, report = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            instance = report["instances"][0]
            self.assertEqual(instance["status"], "flaky")
            first, second = instance["observations"]
            self.assertNotIn("retry_of", first)
            self.assertNotIn("root_run_id", first)
            self.assertNotIn("attempt", first)
            self.assertEqual(second["retry_of"], "r1")
            self.assertEqual(second["root_run_id"], "r1")
            self.assertEqual(second["attempt"], 1)

    def test_validation_errors(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})

            bad_payloads = [
                [],
                "r1",
                123,
                None,
                {},
                {"run_ids": ["r1", "r1"], "extra": 1},
                {"run_ids": "r1"},
                {"run_ids": None},
                {"run_ids": 1},
                {"run_ids": {}},
                {"run_ids": []},
                {"run_ids": ["r1"]},
                {"run_ids": [f"r{i}" for i in range(101)]},
                {"run_ids": ["", "r1"]},
                {"run_ids": ["r1", "r1"]},
                {"run_ids": ["r1", 1]},
                {"run_ids": [None, "r1"]},
                {"run_ids": [["r1"], "r2"]},
            ]
            for payload in bad_payloads:
                status, body = server.json(
                    "POST", "/v1/reports/stability", payload
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(
                    body["error"]["code"], "validation_error", payload
                )

            status, body = server.json(
                "POST", "/v1/reports/stability", raw_body=b"{not json"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

            # Exactly 100 unique entries is acceptable.
            run_ids = ["r1"]
            for index in range(99):
                run_id = f"b{index}"
                server.json("POST", "/v1/runs", {"id": run_id, "case_ids": ["c1"]})
                finish_with(server, run_id, {"c1[0]": "passed"})
                run_ids.append(run_id)
            status, report = stability(server, run_ids)
            self.assertEqual(status, 200)
            self.assertEqual(report["run_count"], 100)
            self.assertEqual(len(report["instances"]), 1)
            self.assertEqual(report["instances"][0]["summary"]["total"], 100)

    def test_missing_and_incomplete_runs_checked_in_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "done", "case_ids": ["c1"]})
            finish_with(server, "done", {"c1[0]": "passed"})
            server.json("POST", "/v1/runs", {"id": "open", "case_ids": ["c1"]})

            status, body = stability(server, ["done", "ghost"])
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            status, body = stability(server, ["done", "open"])
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # The first offending run in request order decides the error.
            status, body = stability(server, ["ghost", "open"])
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")
            status, body = stability(server, ["open", "ghost"])
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

    def test_stability_is_read_only_and_deterministic(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish_with(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "passed"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c2"]})
            finish_with(server, "r2", {"c2[0]": "failed", "c2[1]": "passed"})
            _, before_r1 = server.json("GET", "/v1/runs/r1")
            _, before_r2 = server.json("GET", "/v1/runs/r2")

            status, first = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            status, second = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            self.assertEqual(first, second)

            _, after_r1 = server.json("GET", "/v1/runs/r1")
            _, after_r2 = server.json("GET", "/v1/runs/r2")
            self.assertEqual(before_r1, after_r1)
            self.assertEqual(before_r2, after_r2)
            _, cases = server.json("GET", "/v1/cases")
            self.assertEqual(len(cases), 3)

    def test_restart_clears_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            for run_id in ("r1", "r2"):
                server.json("POST", "/v1/runs", {"id": run_id, "case_ids": ["c1"]})
                finish_with(server, run_id, {"c1[0]": "passed"})
            status, _ = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)

        with ServerHarness(service_module.Service()) as server:
            status, body = stability(server, ["r1", "r2"])
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

    def test_unknown_route_still_not_found(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST", "/v1/reports/stability/extra", {"run_ids": ["r1", "r2"]}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")
            status, body = server.json(
                "GET", "/v1/reports/stability"
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
