"""End-to-end HTTP tests for per-instance stability analysis."""

from __future__ import annotations

import unittest

import testlattice.service as service_module
from test_retries import finish_with
from test_runs import ServerHarness, case_payload, seed_cases


def stability(server: ServerHarness, run_ids: list[str]) -> tuple[int, dict]:
    return server.json("POST", "/v1/reports/stability", {"run_ids": run_ids})


class StabilityTest(unittest.TestCase):
    def test_instances_ordered_by_first_appearance_in_request_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish_with(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "passed"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "failed"})

            status, report = stability(server, ["r2", "r1"])
            self.assertEqual(status, 200)
            self.assertEqual(report["run_count"], 2)
            self.assertEqual(
                report["summary"],
                {"total": 3, "stable_pass": 0, "stable_fail": 0, "flaky": 1, "insufficient": 2},
            )

            instances = report["instances"]
            # r2 is scanned first, so c1[0] appears before c2's instances.
            self.assertEqual(
                [(i["case_id"], i["instance_id"]) for i in instances],
                [("c1", "c1[0]"), ("c2", "c2[0]"), ("c2", "c2[1]")],
            )

            c1 = instances[0]
            self.assertEqual(c1["case_name"], "name-c1")
            self.assertEqual(c1["status"], "flaky")
            self.assertEqual(c1["pass_rate"], 0.5)
            self.assertEqual(
                c1["summary"],
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
                c1["observations"],
                [
                    {"run_id": "r2", "outcome": "failed", "duration_ms": 5},
                    {"run_id": "r1", "outcome": "passed", "duration_ms": 5},
                ],
            )

            # c2's instances are absent from r2; no observation is padded.
            c2_0 = instances[1]
            self.assertEqual(c2_0["status"], "insufficient")
            self.assertEqual(c2_0["pass_rate"], 1.0)
            self.assertEqual(len(c2_0["observations"]), 1)
            self.assertEqual(c2_0["observations"][0]["run_id"], "r1")

            c2_1 = instances[2]
            self.assertEqual(c2_1["status"], "insufficient")
            self.assertEqual(c2_1["pass_rate"], 0.0)
            self.assertEqual(
                c2_1["observations"],
                [{"run_id": "r1", "outcome": "failed", "duration_ms": 5}],
            )

    def test_status_classification(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            for case_id in ("t1", "t2", "t3", "t4", "t5"):
                server.json("POST", "/v1/cases", case_payload(case_id))
            server.json(
                "POST",
                "/v1/runs",
                {"id": "ra", "case_ids": ["t1", "t2", "t3", "t4", "t5"]},
            )
            finish_with(
                server,
                "ra",
                {
                    "t1[0]": "passed",
                    "t2[0]": "failed",
                    "t3[0]": "passed",
                    "t4[0]": "skipped",
                    "t5[0]": "error",
                },
            )
            server.json(
                "POST",
                "/v1/runs",
                {"id": "rb", "case_ids": ["t1", "t2", "t3", "t4", "t5"]},
            )
            finish_with(
                server,
                "rb",
                {
                    "t1[0]": "passed",
                    "t2[0]": "error",
                    "t3[0]": "failed",
                    "t4[0]": "skipped",
                    "t5[0]": "error",
                },
            )

            status, report = stability(server, ["ra", "rb"])
            self.assertEqual(status, 200)
            by_id = {i["case_id"]: i for i in report["instances"]}
            self.assertEqual(by_id["t1"]["status"], "stable_pass")
            self.assertEqual(by_id["t1"]["pass_rate"], 1.0)
            # failed and error together still count as stable_fail.
            self.assertEqual(by_id["t2"]["status"], "stable_fail")
            self.assertEqual(by_id["t2"]["pass_rate"], 0.0)
            self.assertEqual(by_id["t3"]["status"], "flaky")
            self.assertEqual(by_id["t3"]["pass_rate"], 0.5)
            # Skipped observations are not effective; two skips are insufficient.
            self.assertEqual(by_id["t4"]["status"], "insufficient")
            self.assertEqual(by_id["t4"]["pass_rate"], None)
            self.assertEqual(
                by_id["t4"]["summary"],
                {
                    "total": 2,
                    "effective": 0,
                    "passed": 0,
                    "failed": 0,
                    "error": 0,
                    "skipped": 2,
                    "duration_ms": 10,
                },
            )
            self.assertEqual(by_id["t5"]["status"], "stable_fail")
            self.assertEqual(
                report["summary"],
                {"total": 5, "stable_pass": 1, "stable_fail": 2, "flaky": 1, "insufficient": 1},
            )

    def test_pass_rate_rounds_half_up_to_two_decimals(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            # Three effective observations, two passed -> 2/3 -> 0.67.
            outcomes = ["passed", "passed", "failed"]
            for index, outcome in enumerate(outcomes):
                run_id = f"r{index}"
                server.json("POST", "/v1/runs", {"id": run_id, "case_ids": ["c1"]})
                finish_with(server, run_id, {"c1[0]": outcome})

            status, report = stability(server, ["r0", "r1", "r2"])
            self.assertEqual(status, 200)
            instance = report["instances"][0]
            self.assertEqual(instance["pass_rate"], 0.67)
            self.assertEqual(instance["status"], "flaky")

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
            self.assertEqual(
                first, {"run_id": "r1", "outcome": "failed", "duration_ms": 5}
            )
            self.assertEqual(
                second,
                {
                    "run_id": "r2",
                    "outcome": "passed",
                    "duration_ms": 5,
                    "retry_of": "r1",
                    "root_run_id": "r1",
                    "attempt": 1,
                },
            )

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
                {"run_ids": ["r1", "r2"], "extra": 1},
                {"run_ids": "r1"},
                {"run_ids": None},
                {"run_ids": 1},
                {"run_ids": {}},
                {"run_ids": []},
                {"run_ids": ["r1"]},
                {"run_ids": [f"r{i}" for i in range(101)]},
                {"run_ids": ["r1", ""]},
                {"run_ids": ["r1", "r1"]},
                {"run_ids": ["r1", 1]},
                {"run_ids": ["r1", None]},
                {"run_ids": ["r1", ["r2"]]},
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
            run_ids = []
            for index in range(100):
                run_id = f"b{index}"
                server.json("POST", "/v1/runs", {"id": run_id, "case_ids": ["c1"]})
                finish_with(server, run_id, {"c1[0]": "passed"})
                run_ids.append(run_id)
            status, report = stability(server, run_ids)
            self.assertEqual(status, 200)
            self.assertEqual(report["run_count"], 100)
            self.assertEqual(report["summary"]["total"], 1)
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
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "failed"})
            _, before = server.json("GET", "/v1/runs/r1")

            status, first = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            status, second = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            self.assertEqual(first, second)

            _, after = server.json("GET", "/v1/runs/r1")
            self.assertEqual(before, after)

            # Catalog changes after the runs completed do not affect output.
            server.request("DELETE", "/v1/cases/c1")
            status, third = stability(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            self.assertEqual(first, third)

    def test_restart_clears_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "passed"})
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
            status, body = server.json("GET", "/v1/reports/stability")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
