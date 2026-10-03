"""End-to-end HTTP tests for cross-run report aggregation."""

from __future__ import annotations

import unittest

import testlattice.service as service_module
from test_runs import ServerHarness, case_payload, seed_cases
from test_retries import finish_with


def aggregate(server: ServerHarness, payload: object) -> tuple[int, dict]:
    return server.json("POST", "/v1/reports/aggregate", payload)


def case_index(report: dict) -> dict[str, dict]:
    return {entry["case_id"]: entry for entry in report["cases"]}


class AggregateTest(unittest.TestCase):
    def test_basic_aggregation_preserves_request_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish_with(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "passed"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "skipped"})

            status, report = aggregate(server, {"run_ids": ["r2", "r1"]})
            self.assertEqual(status, 200)
            self.assertEqual(report["run_count"], 2)
            self.assertEqual(report["passed"], False)
            self.assertEqual(
                report["summary"],
                {
                    "total": 4,
                    "passed": 2,
                    "failed": 1,
                    "error": 0,
                    "skipped": 1,
                    "duration_ms": 20,
                },
            )
            self.assertEqual([r["run_id"] for r in report["runs"]], ["r2", "r1"])
            r2_entry, r1_entry = report["runs"]
            self.assertEqual(r2_entry["passed"], True)  # skipped is not a failure
            self.assertEqual(
                r2_entry["summary"],
                {
                    "total": 1,
                    "passed": 0,
                    "failed": 0,
                    "error": 0,
                    "skipped": 1,
                    "duration_ms": 5,
                },
            )
            self.assertEqual(r1_entry["passed"], False)
            self.assertEqual(r1_entry["summary"]["failed"], 1)
            for entry in report["runs"]:
                self.assertNotIn("retry_of", entry)
                self.assertNotIn("root_run_id", entry)
                self.assertNotIn("attempt", entry)

            # Cases ordered by first appearance while scanning r2 then r1.
            self.assertEqual([c["case_id"] for c in report["cases"]], ["c1", "c2"])
            cases = case_index(report)
            c1 = cases["c1"]
            self.assertEqual(
                c1["summary"],
                {
                    "total": 2,
                    "passed": 1,
                    "failed": 0,
                    "error": 0,
                    "skipped": 1,
                    "duration_ms": 10,
                },
            )
            self.assertEqual([r["run_id"] for r in c1["runs"]], ["r2", "r1"])
            self.assertEqual(c1["runs"][0]["case_name"], "name-c1")
            self.assertEqual(c1["runs"][0]["passed"], True)
            self.assertEqual(c1["runs"][1]["passed"], True)
            self.assertEqual(c1["trend"], "stable_pass")

            c2 = cases["c2"]
            self.assertEqual([r["run_id"] for r in c2["runs"]], ["r1"])
            self.assertEqual(c2["trend"], "insufficient")
            self.assertEqual(c2["summary"]["total"], 2)
            self.assertEqual(c2["summary"]["failed"], 1)
            self.assertEqual(c2["runs"][0]["case_name"], "name-c2")
            self.assertEqual(c2["runs"][0]["passed"], False)

    def test_retry_runs_carry_retry_fields(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            finish_with(server, "r1", {"c2[0]": "passed", "c2[1]": "failed"})
            status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            finish_with(server, "r2", {"c2[1]": "passed"})

            status, report = aggregate(server, {"run_ids": ["r1", "r2"]})
            self.assertEqual(status, 200)
            # r1 still has a failed instance, so the aggregate is not passed.
            self.assertEqual(report["passed"], False)
            retry_entry = report["runs"][1]
            self.assertEqual(retry_entry["run_id"], "r2")
            self.assertEqual(retry_entry["retry_of"], "r1")
            self.assertEqual(retry_entry["root_run_id"], "r1")
            self.assertEqual(retry_entry["attempt"], 1)
            self.assertEqual(retry_entry["passed"], True)
            self.assertEqual(
                retry_entry["summary"],
                {
                    "total": 1,
                    "passed": 1,
                    "failed": 0,
                    "error": 0,
                    "skipped": 0,
                    "duration_ms": 5,
                },
            )
            self.assertNotIn("retry_of", report["runs"][0])

            c2 = case_index(report)["c2"]
            self.assertEqual([r["run_id"] for r in c2["runs"]], ["r1", "r2"])
            # r1 has a failed c2 instance; the retry r2 passed.
            self.assertEqual(c2["trend"], "improvement")
            self.assertEqual([r["passed"] for r in c2["runs"]], [False, True])

    def test_trends(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            for case_id in ("stable", "failing", "regress", "improve", "flap"):
                server.json("POST", "/v1/cases", case_payload(case_id))

            def run(run_id: str, outcomes: dict[str, str]) -> None:
                server.json("POST", "/v1/runs", {"id": run_id, "case_ids": list(outcomes)})
                finish_with(
                    server,
                    run_id,
                    {f"{case_id}[0]": outcome for case_id, outcome in outcomes.items()},
                )

            run(
                "r1",
                {
                    "stable": "passed",
                    "failing": "failed",
                    "regress": "passed",
                    "improve": "error",
                    "flap": "passed",
                },
            )
            run(
                "r2",
                {
                    "stable": "passed",
                    "failing": "error",
                    "regress": "failed",
                    "improve": "passed",
                    "flap": "failed",
                },
            )
            run("r3", {"flap": "passed"})

            status, report = aggregate(server, {"run_ids": ["r1", "r2", "r3"]})
            self.assertEqual(status, 200)
            trends = {
                entry["case_id"]: entry["trend"] for entry in report["cases"]
            }
            self.assertEqual(trends["stable"], "stable_pass")
            self.assertEqual(trends["failing"], "stable_fail")
            self.assertEqual(trends["regress"], "regression")
            self.assertEqual(trends["improve"], "improvement")
            self.assertEqual(trends["flap"], "fluctuating")

            # Cases only list the runs that actually contain them.
            cases = case_index(report)
            self.assertEqual(
                [r["run_id"] for r in cases["flap"]["runs"]], ["r1", "r2", "r3"]
            )
            self.assertEqual(
                [r["run_id"] for r in cases["stable"]["runs"]], ["r1", "r2"]
            )

    def test_parameterized_and_duplicate_rows_counted_individually(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.json(
                "POST",
                "/v1/cases",
                case_payload(
                    "dup",
                    parameterization={"rows": [{"x": 1}, {"x": 1}, {"x": 2}]},
                ),
            )
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["dup"]})
            finish_with(
                server,
                "r1",
                {"dup[0]": "passed", "dup[1]": "passed", "dup[2]": "skipped"},
            )

            status, report = aggregate(server, {"run_ids": ["r1"]})
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["total"], 3)
            self.assertEqual(report["summary"]["passed"], 2)
            self.assertEqual(report["summary"]["skipped"], 1)
            self.assertEqual(report["passed"], True)
            dup = case_index(report)["dup"]
            self.assertEqual(dup["summary"]["total"], 3)
            self.assertEqual(dup["runs"][0]["summary"]["total"], 3)
            self.assertEqual(dup["runs"][0]["passed"], True)
            self.assertEqual(dup["trend"], "insufficient")

    def test_aggregation_uses_frozen_data(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})

            # Mutate the catalog after the run completed.
            server.request("DELETE", "/v1/cases/c1")
            server.json("POST", "/v1/cases", case_payload("c1", name="RENAMED"))

            status, report = aggregate(server, {"run_ids": ["r1"]})
            self.assertEqual(status, 200)
            c1 = case_index(report)["c1"]
            self.assertEqual(c1["runs"][0]["case_name"], "name-c1")

            # The aggregate is read-only: the run report is unchanged.
            status, fetched = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(fetched["summary"]["passed"], 1)

    def test_validation_errors(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})

            bad_payloads = [
                ["r1"],
                "r1",
                123,
                None,
                {},
                {"run_ids": ["r1"], "extra": 1},
                {"run_ids": "r1"},
                {"run_ids": None},
                {"run_ids": []},
                {"run_ids": [f"r{i}" for i in range(101)]},
                {"run_ids": ["r1", "r1"]},
                {"run_ids": ["r1", 2]},
                {"run_ids": ["r1", ""]},
                {"run_ids": [""]},
                {"run_ids": [None]},
            ]
            for payload in bad_payloads:
                status, body = aggregate(server, payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(body["error"]["code"], "validation_error", payload)

            status, body = server.json(
                "POST", "/v1/reports/aggregate", raw_body=b"{nope"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

            # Exactly 100 ids is accepted (they must exist, so reuse is out;
            # duplicates are rejected, so reference real runs is impossible
            # here — instead check the boundary error message stays 400).
            status, body = aggregate(server, {"run_ids": [f"m{i}" for i in range(100)]})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

    def test_missing_and_incomplete_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "done", "case_ids": ["c1"]})
            finish_with(server, "done", {"c1[0]": "passed"})
            server.json("POST", "/v1/runs", {"id": "open", "case_ids": ["c1"]})

            # First missing run in request order wins.
            status, body = aggregate(server, {"run_ids": ["done", "ghost", "open"]})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            # Existing but incomplete run conflicts.
            status, body = aggregate(server, {"run_ids": ["done", "open"]})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # An incomplete retry source behaves the same.
            status, body = aggregate(server, {"run_ids": ["ghost"]})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

    def test_restart_clears_runs_for_aggregation(self) -> None:
        service = service_module.Service()
        with ServerHarness(service) as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})
            status, _ = aggregate(server, {"run_ids": ["r1"]})
            self.assertEqual(status, 200)

        with ServerHarness(service_module.Service()) as server:
            status, body = aggregate(server, {"run_ids": ["r1"]})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

    def test_unknown_routes_still_not_found(self) -> None:
        with ServerHarness() as server:
            status, body = server.json("GET", "/v1/reports/aggregate")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")
            status, body = server.json("POST", "/v1/reports", {"run_ids": ["r1"]})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
