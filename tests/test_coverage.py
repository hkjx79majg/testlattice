"""End-to-end HTTP tests for line-coverage fragments and merged reports."""

from __future__ import annotations

import unittest

from test_runs import ServerHarness, seed_cases


def submit(server: ServerHarness, run_id: str, instance_id: str, **extra: object) -> tuple[int, dict]:
    payload = {"instance_id": instance_id, "outcome": "passed", "duration_ms": 5}
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def complete(server: ServerHarness, run_id: str) -> None:
    status, _ = server.json("POST", f"/v1/runs/{run_id}/complete")
    assert status == 200


class CoverageTest(unittest.TestCase):
    def test_submit_with_coverage_and_merged_report(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})

            status, report = submit(
                server,
                "r1",
                "c2[0]",
                coverage={
                    "files": {
                        "b.py": {"executable_lines": [3, 1, 2], "covered_lines": [1]},
                        "a.py": {"executable_lines": [10, 20], "covered_lines": []},
                    }
                },
            )
            self.assertEqual(status, 200)
            # The run report structure is unchanged by coverage fragments.
            self.assertNotIn("coverage", report["instances"][0])

            status, _ = submit(
                server,
                "r1",
                "c2[1]",
                coverage={
                    "files": {
                        "b.py": {"executable_lines": [2, 4], "covered_lines": [2, 4]},
                    }
                },
            )
            self.assertEqual(status, 200)
            status, _ = submit(server, "r1", "c1[0]")
            self.assertEqual(status, 200)
            complete(server, "r1")

            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(coverage["run_id"], "r1")
            self.assertEqual(
                [f["path"] for f in coverage["files"]], ["a.py", "b.py"]
            )
            a, b = coverage["files"]
            self.assertEqual(a["executable_lines"], [10, 20])
            self.assertEqual(a["covered_lines"], [])
            self.assertEqual(a["missed_lines"], [10, 20])
            self.assertEqual(a["coverage_percent"], 0.0)
            self.assertEqual(b["executable_lines"], [1, 2, 3, 4])
            self.assertEqual(b["covered_lines"], [1, 2, 4])
            self.assertEqual(b["missed_lines"], [3])
            self.assertEqual(b["coverage_percent"], 75.0)
            self.assertEqual(
                coverage["summary"],
                {
                    "files": 2,
                    "executable_lines": 6,
                    "covered_lines": 3,
                    "missed_lines": 3,
                    "coverage_percent": 50.0,
                },
            )

            # Repeated reads are stable and read-only.
            status, again = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(again, coverage)
            _, report = server.json("GET", "/v1/runs/r1")
            self.assertNotIn("coverage", report["instances"][0])

    def test_no_fragments_gives_empty_report(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(server, "r1", "c1[0]")
            complete(server, "r1")

            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(
                coverage,
                {
                    "run_id": "r1",
                    "summary": {
                        "files": 0,
                        "executable_lines": 0,
                        "covered_lines": 0,
                        "missed_lines": 0,
                        "coverage_percent": None,
                    },
                    "files": [],
                },
            )

    def test_percentage_rounding(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(
                server,
                "r1",
                "c1[0]",
                coverage={
                    "files": {
                        # 1/3 -> 33.33, 2/3 -> 66.67, exact half rounds up.
                        "x.py": {"executable_lines": [1, 2, 3], "covered_lines": [1]},
                        "y.py": {"executable_lines": [1, 2, 3], "covered_lines": [1, 2]},
                        "z.py": {
                            "executable_lines": list(range(1, 33)),
                            "covered_lines": [1],
                        },
                    }
                },
            )
            complete(server, "r1")
            _, coverage = server.json("GET", "/v1/runs/r1/coverage")
            by_path = {f["path"]: f for f in coverage["files"]}
            self.assertEqual(by_path["x.py"]["coverage_percent"], 33.33)
            self.assertEqual(by_path["y.py"]["coverage_percent"], 66.67)
            # 1/32 = 3.125 -> round half up to 3.13.
            self.assertEqual(by_path["z.py"]["coverage_percent"], 3.13)
            # Totals: 4 covered of 38 executable -> 10.53.
            self.assertEqual(coverage["summary"]["coverage_percent"], 10.53)

    def test_validation_errors_leave_instance_pending(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

            bad_coverages = [
                [],
                "x",
                {},
                {"files": None},
                {"files": {}},
                {"files": [], },
                {"files": {"a.py": []}},
                {"files": {"a.py": {}}},
                {"files": {"a.py": {"executable_lines": [1]}}},
                {"files": {"a.py": {"covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [1], "covered_lines": [], "x": 1}}},
                {"files": {"a.py": {"executable_lines": [0], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [-1], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [1.5], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [True], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": ["1"], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [1, 1], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [1], "covered_lines": [2]}}},
                {"files": {"a.py": {"executable_lines": [1], "covered_lines": [1, 1]}}},
                {"files": {"": {"executable_lines": [1], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [1], "covered_lines": []}}, "x": 1},
            ]
            for coverage in bad_coverages:
                status, body = submit(server, "r1", "c1[0]", coverage=coverage)
                self.assertEqual(status, 400, coverage)
                self.assertEqual(body["error"]["code"], "validation_error", coverage)

            # File and executable-line limits.
            too_many_files = {
                "files": {
                    f"f{i}.py": {"executable_lines": [1], "covered_lines": []}
                    for i in range(1001)
                }
            }
            status, body = submit(server, "r1", "c1[0]", coverage=too_many_files)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            too_many_lines = {
                "files": {
                    "big.py": {
                        "executable_lines": list(range(1, 100002)),
                        "covered_lines": [],
                    }
                }
            }
            status, body = submit(server, "r1", "c1[0]", coverage=too_many_lines)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            # Nothing was stored: the instance is still pending and accepts a
            # clean submission afterwards.
            _, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(report["instances"][0]["outcome"], "pending")
            status, _ = submit(server, "r1", "c1[0]")
            self.assertEqual(status, 200)
            complete(server, "r1")
            _, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(coverage["files"], [])
            self.assertEqual(coverage["summary"]["coverage_percent"], None)

    def test_null_coverage_behaves_like_absent(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, report = submit(server, "r1", "c1[0]", coverage=None)
            self.assertEqual(status, 200)
            self.assertNotIn("coverage", report["instances"][0])

    def test_missing_and_incomplete_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            status, body = server.json("GET", "/v1/runs/ghost/coverage")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, body = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

    def test_retry_does_not_inherit_coverage(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(
                server,
                "r1",
                "c1[0]",
                outcome="failed",
                coverage={
                    "files": {
                        "a.py": {"executable_lines": [1, 2], "covered_lines": [1]}
                    }
                },
            )
            complete(server, "r1")

            status, report = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            submit(
                server,
                "r2",
                "c1[0]",
                coverage={
                    "files": {
                        "b.py": {"executable_lines": [5], "covered_lines": [5]}
                    }
                },
            )
            complete(server, "r2")

            _, coverage = server.json("GET", "/v1/runs/r2/coverage")
            self.assertEqual([f["path"] for f in coverage["files"]], ["b.py"])
            self.assertEqual(coverage["summary"]["executable_lines"], 1)

            # The source run's own report is untouched by the retry.
            _, source = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual([f["path"] for f in source["files"]], ["a.py"])


if __name__ == "__main__":
    unittest.main()
