"""End-to-end HTTP tests for cross-run report aggregation."""

from __future__ import annotations

import unittest

import testlattice.service as service_module
from test_retries import finish_with
from test_runs import ServerHarness, case_payload, seed_cases


def seed_named_cases(server: ServerHarness, *case_ids: str) -> None:
    server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
    for case_id in case_ids:
        server.json("POST", "/v1/cases", case_payload(case_id))


def aggregate(server: ServerHarness, run_ids: list[str]) -> tuple[int, dict]:
    return server.json("POST", "/v1/reports/aggregate", {"run_ids": run_ids})


class AggregateTest(unittest.TestCase):
    def test_aggregates_runs_in_request_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish_with(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "skipped"},
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "passed"})

            status, report = aggregate(server, ["r2", "r1"])
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

            runs = report["runs"]
            self.assertEqual([r["run_id"] for r in runs], ["r2", "r1"])
            self.assertEqual(runs[0]["passed"], True)
            self.assertEqual(
                runs[0]["summary"],
                {
                    "total": 1,
                    "passed": 1,
                    "failed": 0,
                    "error": 0,
                    "skipped": 0,
                    "duration_ms": 5,
                },
            )
            self.assertEqual(runs[1]["passed"], False)
            self.assertEqual(
                runs[1]["summary"],
                {
                    "total": 3,
                    "passed": 1,
                    "failed": 1,
                    "error": 0,
                    "skipped": 1,
                    "duration_ms": 15,
                },
            )
            for entry in runs:
                self.assertNotIn("retry_of", entry)
                self.assertNotIn("root_run_id", entry)
                self.assertNotIn("attempt", entry)

            # c1 appears first because r2 (scanned first) contains it.
            cases = report["cases"]
            self.assertEqual([c["case_id"] for c in cases], ["c1", "c2"])

            c1 = cases[0]
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
            self.assertEqual(c1["trend"], "stable_pass")
            self.assertEqual([r["run_id"] for r in c1["runs"]], ["r2", "r1"])
            for entry in c1["runs"]:
                self.assertEqual(entry["case_name"], "name-c1")
                self.assertEqual(entry["passed"], True)
            self.assertEqual(
                c1["runs"][0]["summary"],
                {
                    "total": 1,
                    "passed": 1,
                    "failed": 0,
                    "error": 0,
                    "skipped": 0,
                    "duration_ms": 5,
                },
            )
            self.assertEqual(
                c1["runs"][1]["summary"],
                {
                    "total": 1,
                    "passed": 0,
                    "failed": 0,
                    "error": 0,
                    "skipped": 1,
                    "duration_ms": 5,
                },
            )

            c2 = cases[1]
            self.assertEqual(
                c2["summary"],
                {
                    "total": 2,
                    "passed": 1,
                    "failed": 1,
                    "error": 0,
                    "skipped": 0,
                    "duration_ms": 10,
                },
            )
            self.assertEqual(c2["trend"], "insufficient")
            self.assertEqual(len(c2["runs"]), 1)
            self.assertEqual(c2["runs"][0]["run_id"], "r1")
            self.assertEqual(c2["runs"][0]["case_name"], "name-c2")
            self.assertEqual(c2["runs"][0]["passed"], False)

    def test_parameterized_instances_counted_individually(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            finish_with(server, "r1", {"c2[0]": "passed", "c2[1]": "failed"})

            status, report = aggregate(server, ["r1"])
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["total"], 2)
            self.assertEqual(report["summary"]["passed"], 1)
            self.assertEqual(report["summary"]["failed"], 1)
            self.assertEqual(report["passed"], False)
            c2 = report["cases"][0]
            self.assertEqual(c2["case_id"], "c2")
            self.assertEqual(c2["summary"]["total"], 2)
            self.assertEqual(c2["runs"][0]["summary"]["total"], 2)
            self.assertEqual(c2["runs"][0]["passed"], False)

    def test_error_outcome_marks_run_and_case_failed(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "error"})

            status, report = aggregate(server, ["r1"])
            self.assertEqual(status, 200)
            self.assertEqual(report["passed"], False)
            self.assertEqual(report["summary"]["error"], 1)
            self.assertEqual(report["runs"][0]["passed"], False)
            self.assertEqual(report["cases"][0]["runs"][0]["passed"], False)

    def test_skipped_is_not_a_failure(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "skipped"})

            status, report = aggregate(server, ["r1"])
            self.assertEqual(status, 200)
            self.assertEqual(report["passed"], True)
            self.assertEqual(report["runs"][0]["passed"], True)
            self.assertEqual(report["cases"][0]["runs"][0]["passed"], True)

    def test_retry_runs_carry_retry_fields(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed"})
            server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            finish_with(server, "r2", {"c1[0]": "passed"})

            status, report = aggregate(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            first, second = report["runs"]
            self.assertNotIn("retry_of", first)
            self.assertEqual(second["retry_of"], "r1")
            self.assertEqual(second["root_run_id"], "r1")
            self.assertEqual(second["attempt"], 1)

            c1 = report["cases"][0]
            self.assertEqual(c1["trend"], "improvement")
            self.assertEqual(
                [entry["passed"] for entry in c1["runs"]], [False, True]
            )

    def test_trend_classification(self) -> None:
        with ServerHarness() as server:
            seed_named_cases(server, "t1", "t2", "t3", "t4", "t5")
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
                    "t4[0]": "failed",
                    "t5[0]": "passed",
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
                    "t2[0]": "failed",
                    "t3[0]": "failed",
                    "t4[0]": "passed",
                    "t5[0]": "failed",
                },
            )
            server.json("POST", "/v1/runs", {"id": "rc", "case_ids": ["t5"]})
            finish_with(server, "rc", {"t5[0]": "passed"})

            status, report = aggregate(server, ["ra", "rb", "rc"])
            self.assertEqual(status, 200)
            trends = {c["case_id"]: c["trend"] for c in report["cases"]}
            self.assertEqual(
                trends,
                {
                    "t1": "stable_pass",
                    "t2": "stable_fail",
                    "t3": "regression",
                    "t4": "improvement",
                    "t5": "fluctuating",
                },
            )
            # t5 appears in all three runs; the others only in ra and rb.
            by_id = {c["case_id"]: c for c in report["cases"]}
            self.assertEqual(
                [r["run_id"] for r in by_id["t5"]["runs"]], ["ra", "rb", "rc"]
            )
            self.assertEqual(
                [r["run_id"] for r in by_id["t1"]["runs"]], ["ra", "rb"]
            )

    def test_case_name_is_frozen_per_run(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})

            server.request("DELETE", "/v1/cases/c1")
            server.json("POST", "/v1/cases", case_payload("c1", name="renamed"))
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            finish_with(server, "r2", {"c1[0]": "passed"})

            status, report = aggregate(server, ["r1", "r2"])
            self.assertEqual(status, 200)
            c1 = report["cases"][0]
            self.assertEqual(
                [entry["case_name"] for entry in c1["runs"]],
                ["name-c1", "renamed"],
            )

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
                {"run_ids": ["r1"], "extra": 1},
                {"run_ids": "r1"},
                {"run_ids": None},
                {"run_ids": 1},
                {"run_ids": {}},
                {"run_ids": []},
                {"run_ids": [f"r{i}" for i in range(101)]},
                {"run_ids": [""]},
                {"run_ids": ["r1", "r1"]},
                {"run_ids": ["r1", 1]},
                {"run_ids": [None]},
                {"run_ids": [["r1"]]},
            ]
            for payload in bad_payloads:
                status, body = server.json(
                    "POST", "/v1/reports/aggregate", payload
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(
                    body["error"]["code"], "validation_error", payload
                )

            status, body = server.json(
                "POST", "/v1/reports/aggregate", raw_body=b"{not json"
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
            status, report = aggregate(server, run_ids)
            self.assertEqual(status, 200)
            self.assertEqual(report["run_count"], 100)
            self.assertEqual(report["summary"]["total"], 100)

    def test_missing_and_incomplete_runs_checked_in_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "done", "case_ids": ["c1"]})
            finish_with(server, "done", {"c1[0]": "passed"})
            server.json("POST", "/v1/runs", {"id": "open", "case_ids": ["c1"]})

            status, body = aggregate(server, ["ghost"])
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            status, body = aggregate(server, ["done", "ghost"])
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            status, body = aggregate(server, ["open"])
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # The first offending run in analysis order decides the error.
            status, body = aggregate(server, ["ghost", "open"])
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")
            status, body = aggregate(server, ["open", "ghost"])
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

    def test_aggregate_is_read_only(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish_with(
                server,
                "r1",
                {"c2[0]": "passed", "c2[1]": "failed", "c1[0]": "passed"},
            )
            _, before = server.json("GET", "/v1/runs/r1")

            status, first = aggregate(server, ["r1"])
            self.assertEqual(status, 200)
            status, second = aggregate(server, ["r1"])
            self.assertEqual(status, 200)
            self.assertEqual(first, second)

            _, after = server.json("GET", "/v1/runs/r1")
            self.assertEqual(before, after)
            _, cases = server.json("GET", "/v1/cases")
            self.assertEqual(len(cases), 3)

    def test_restart_clears_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed"})
            status, _ = aggregate(server, ["r1"])
            self.assertEqual(status, 200)

        with ServerHarness(service_module.Service()) as server:
            status, body = aggregate(server, ["r1"])
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

    def test_unknown_route_still_not_found(self) -> None:
        with ServerHarness() as server:
            status, body = server.json("POST", "/v1/reports", {"run_ids": ["r1"]})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")
            status, body = server.json(
                "POST", "/v1/reports/aggregate/extra", {"run_ids": ["r1"]}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
