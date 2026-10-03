"""End-to-end HTTP tests for per-instance coverage fragments and merged
coverage reports on completed runs."""

from __future__ import annotations

import json
import unittest

from test_runs import ServerHarness, seed_cases


def submit(
    server: ServerHarness,
    run_id: str,
    instance_id: str,
    outcome: str = "passed",
    **extra: object,
) -> tuple[int, dict]:
    payload: dict = {
        "instance_id": instance_id,
        "outcome": outcome,
        "duration_ms": 5,
    }
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


class CoverageTest(unittest.TestCase):
    def test_merges_fragments_with_union_and_ordering(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})

            status, _ = submit(
                server,
                "r1",
                "c2[0]",
                coverage={
                    "files": {
                        # Request order is unsorted; the report must sort.
                        "b.py": {"executable_lines": [3, 1, 5], "covered_lines": [3, 1]},
                        "a.py": {"executable_lines": [10, 20], "covered_lines": []},
                    }
                },
            )
            self.assertEqual(status, 200)
            status, _ = submit(
                server,
                "r1",
                "c2[1]",
                outcome="failed",
                coverage={
                    "files": {
                        "b.py": {"executable_lines": [5, 7], "covered_lines": [5, 7]},
                        "c.py": {"executable_lines": [2], "covered_lines": []},
                    }
                },
            )
            self.assertEqual(status, 200)

            # The run report itself never carries coverage data.
            status, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            for instance in report["instances"]:
                self.assertNotIn("coverage", instance)

            # Coverage is unavailable before completion.
            status, body = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            status, _ = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 200)
            status, content_type, raw_a = server.request(
                "GET", "/v1/runs/r1/coverage"
            )
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json; charset=utf-8")
            coverage = json.loads(raw_a)

            self.assertEqual(coverage["run_id"], "r1")
            self.assertEqual(
                [entry["path"] for entry in coverage["files"]],
                ["a.py", "b.py", "c.py"],
            )
            self.assertEqual(
                coverage["files"][0],
                {
                    "path": "a.py",
                    "executable_lines": [10, 20],
                    "covered_lines": [],
                    "missed_lines": [10, 20],
                    "coverage_percent": 0.0,
                },
            )
            self.assertEqual(
                coverage["files"][1],
                {
                    "path": "b.py",
                    "executable_lines": [1, 3, 5, 7],
                    "covered_lines": [1, 3, 5, 7],
                    "missed_lines": [],
                    "coverage_percent": 100.0,
                },
            )
            self.assertEqual(
                coverage["files"][2],
                {
                    "path": "c.py",
                    "executable_lines": [2],
                    "covered_lines": [],
                    "missed_lines": [2],
                    "coverage_percent": 0.0,
                },
            )
            # a.py: 2 executable / 0 covered; b.py: 4 / 4; c.py: 1 / 0.
            self.assertEqual(
                coverage["summary"],
                {
                    "files": 3,
                    "executable_lines": 7,
                    "covered_lines": 4,
                    "missed_lines": 3,
                    "coverage_percent": 57.14,
                },
            )

            # Repeated reads are byte-identical and do not mutate the run.
            status, _, raw_b = server.request("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(raw_b, raw_a)
            status, run_report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(run_report["status"], "completed")

    def test_rounding_half_up_to_two_decimals(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            submit(
                server,
                "r1",
                "c2[0]",
                coverage={
                    "files": {
                        "thirds.py": {
                            "executable_lines": [1, 2, 3],
                            "covered_lines": [1],
                        }
                    }
                },
            )
            submit(
                server,
                "r1",
                "c2[1]",
                coverage={
                    "files": {
                        "thirds.py": {
                            "executable_lines": [1, 2, 3],
                            "covered_lines": [3],
                        }
                    }
                },
            )
            server.json("POST", "/v1/runs/r1/complete")
            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            entry = coverage["files"][0]
            self.assertEqual(entry["covered_lines"], [1, 3])
            self.assertEqual(entry["missed_lines"], [2])
            self.assertEqual(entry["coverage_percent"], 66.67)
            self.assertEqual(coverage["summary"]["coverage_percent"], 66.67)

    def test_unicode_codepoint_file_ordering(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(
                server,
                "r1",
                "c1[0]",
                coverage={
                    "files": {
                        "中.py": {"executable_lines": [1], "covered_lines": []},
                        "a.py": {"executable_lines": [1], "covered_lines": []},
                        "A.py": {"executable_lines": [1], "covered_lines": []},
                        "b": {"executable_lines": [1], "covered_lines": []},
                    }
                },
            )
            server.json("POST", "/v1/runs/r1/complete")
            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(
                [entry["path"] for entry in coverage["files"]],
                ["A.py", "a.py", "b", "中.py"],
            )

    def test_completed_run_without_fragments_is_empty(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            # Existing behavior: submissions without coverage stay valid.
            self.assertEqual(submit(server, "r1", "c2[0]")[0], 200)
            self.assertEqual(submit(server, "r1", "c2[1]", outcome="skipped")[0], 200)
            self.assertEqual(submit(server, "r1", "c1[0]")[0], 200)
            status, _ = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 200)

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

    def test_missing_and_incomplete_run_status(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

            status, body = server.json("GET", "/v1/runs/ghost/coverage")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            status, body = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

    def test_invalid_fragments_rejected_atomically(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})

            valid_fragment = {
                "files": {
                    "keep.py": {"executable_lines": [4, 5, 6], "covered_lines": [4]}
                }
            }
            bad_fragments = [
                None,
                [],
                "x",
                42,
                {},
                {"files": {}},
                {"files": []},
                {"files": None},
                {"files": {"a.py": None}},
                {"files": {"a.py": "nope"}},
                {"files": {"a.py": {}}},
                {"files": {"a.py": {"executable_lines": [1]}}},
                {"files": {"a.py": {"covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": "1", "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [0], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [-2], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [1.5], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [True], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [1, 1], "covered_lines": []}}},
                {"files": {"a.py": {"executable_lines": [2], "covered_lines": [1]}}},
                {"files": {"a.py": {"executable_lines": [2], "covered_lines": [2, 2]}}},
                {"files": {"a.py": {"executable_lines": [2], "covered_lines": [True]}}},
                {
                    "files": {
                        "a.py": {"executable_lines": [2], "covered_lines": [2]}
                    },
                    "zz": 1,
                },
                {
                    "files": {
                        "a.py": {
                            "executable_lines": [2],
                            "covered_lines": [2],
                            "x": 1,
                        }
                    }
                },
                {"files": {"": {"executable_lines": [1], "covered_lines": []}}},
            ]
            for fragment in bad_fragments:
                status, body = submit(server, "r1", "c2[0]", coverage=fragment)
                self.assertEqual(status, 400, fragment)
                self.assertEqual(body["error"]["code"], "validation_error", fragment)

            # Malformed JSON keeps the invalid_json contract.
            status, body = server.json(
                "POST",
                "/v1/runs/r1/results",
                raw_body=b'{"instance_id": "c2[0]", "coverage": ',
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

            # Nothing was saved: both instances stay pending.
            status, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["pending"], 2)
            self.assertTrue(
                all(i["outcome"] == "pending" for i in report["instances"])
            )

            # A rejected fragment must not have been partially merged: submit
            # a different valid fragment for the same instance, then complete
            # via the second instance and expect only the valid fragment.
            status, _ = submit(
                server, "r1", "c2[0]", coverage=valid_fragment
            )
            self.assertEqual(status, 200)
            status, _ = submit(server, "r1", "c2[1]")
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/r1/complete")
            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual([f["path"] for f in coverage["files"]], ["keep.py"])
            self.assertEqual(coverage["files"][0]["executable_lines"], [4, 5, 6])
            self.assertEqual(coverage["files"][0]["covered_lines"], [4])
            self.assertEqual(coverage["summary"]["files"], 1)

    def test_unknown_result_field_rejected_even_with_valid_coverage(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, body = server.json(
                "POST",
                "/v1/runs/r1/results",
                {
                    "instance_id": "c1[0]",
                    "outcome": "passed",
                    "duration_ms": 1,
                    "coverage": {"files": {"a": {"executable_lines": [1], "covered_lines": []}}},
                    "bogus": True,
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
            status, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(report["summary"]["pending"], 1)

    def test_instance_lookup_still_404_with_valid_fragment(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            status, body = submit(
                server,
                "r1",
                "c1[0]",
                coverage={
                    "files": {"a.py": {"executable_lines": [1], "covered_lines": [1]}}
                },
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "instance_not_found")
            # Finish without coverage: the orphan fragment was never stored.
            submit(server, "r1", "c2[0]")
            submit(server, "r1", "c2[1]")
            server.json("POST", "/v1/runs/r1/complete")
            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(coverage["files"], [])
            self.assertIsNone(coverage["summary"]["coverage_percent"])

    def test_file_and_line_limits_apply_per_fragment(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)

            # File cap: exactly 1000 accepted, 1001 rejected.
            server.json("POST", "/v1/runs", {"id": "rf", "case_ids": ["c2"]})
            thousand = {
                "files": {
                    f"f{i:04d}.py": {
                        "executable_lines": [1],
                        "covered_lines": [1],
                    }
                    for i in range(1000)
                }
            }
            status, _ = submit(server, "rf", "c2[0]", coverage=thousand)
            self.assertEqual(status, 200)

            thousand_and_one = {
                "files": {
                    f"g{i:04d}.py": {
                        "executable_lines": [1],
                        "covered_lines": [],
                    }
                    for i in range(1001)
                }
            }
            status, body = submit(server, "rf", "c2[1]", coverage=thousand_and_one)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            # Rejected fragment left the instance pending; a valid fragment
            # for it is still accepted, so the merged run may hold >1000
            # files — the cap bounds each fragment, not the merged report.
            status, _ = submit(
                server,
                "rf",
                "c2[1]",
                coverage={"files": {"one-more.py": {"executable_lines": [1], "covered_lines": []}}},
            )
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/rf/complete")
            status, coverage = server.json("GET", "/v1/runs/rf/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(coverage["summary"]["files"], 1001)

            # Executable-line cap: 100001 rejected; 100000 across files accepted.
            server.json("POST", "/v1/runs", {"id": "rl", "case_ids": ["c2"]})
            over = {
                "files": {
                    "big.py": {
                        "executable_lines": list(range(1, 100002)),
                        "covered_lines": [],
                    }
                }
            }
            status, body = submit(server, "rl", "c2[0]", coverage=over)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            boundary = {
                "files": {
                    "p.py": {
                        "executable_lines": list(range(1, 50001)),
                        "covered_lines": [],
                    },
                    "q.py": {
                        "executable_lines": list(range(50001, 100001)),
                        "covered_lines": [],
                    },
                }
            }
            status, _ = submit(server, "rl", "c2[0]", coverage=boundary)
            self.assertEqual(status, 200)
            status, _ = submit(server, "rl", "c2[1]")
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/rl/complete")
            status, coverage = server.json("GET", "/v1/runs/rl/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(coverage["summary"]["executable_lines"], 100000)
            self.assertEqual(coverage["summary"]["covered_lines"], 0)
            self.assertEqual(coverage["summary"]["coverage_percent"], 0.0)

    def test_retry_does_not_inherit_source_coverage(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            submit(
                server,
                "r1",
                "c2[0]",
                outcome="failed",
                coverage={
                    "files": {"a.py": {"executable_lines": [1, 2], "covered_lines": [1]}}
                },
            )
            submit(
                server,
                "r1",
                "c2[1]",
                outcome="failed",
                coverage={
                    "files": {"a.py": {"executable_lines": [2, 3], "covered_lines": [3]}}
                },
            )
            server.json("POST", "/v1/runs/r1/complete")

            status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)

            # Incomplete retry behaves like any open run.
            status, body = server.json("GET", "/v1/runs/r2/coverage")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # Retry instances submit fresh results: one passes without a
            # fragment, one fails with a brand-new fragment.
            self.assertEqual(submit(server, "r2", "c2[0]", outcome="passed")[0], 200)
            self.assertEqual(
                submit(
                    server,
                    "r2",
                    "c2[1]",
                    outcome="failed",
                    coverage={
                        "files": {
                            "b.py": {"executable_lines": [7, 8], "covered_lines": [7]}
                        }
                    },
                )[0],
                200,
            )
            server.json("POST", "/v1/runs/r2/complete")

            # r2 aggregates only what its own instances submitted.
            status, coverage = server.json("GET", "/v1/runs/r2/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(
                coverage["files"],
                [
                    {
                        "path": "b.py",
                        "executable_lines": [7, 8],
                        "covered_lines": [7],
                        "missed_lines": [8],
                        "coverage_percent": 50.0,
                    }
                ],
            )

            # The source run's fragments are untouched.
            status, source = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(
                source["files"][0],
                {
                    "path": "a.py",
                    "executable_lines": [1, 2, 3],
                    "covered_lines": [1, 3],
                    "missed_lines": [2],
                    "coverage_percent": 66.67,
                },
            )

            # A second-generation retry starts empty again, even though both
            # its source and root carry fragments.
            status, r3 = server.json("POST", "/v1/runs/r2/retry", {"id": "r3"})
            self.assertEqual(status, 201)
            self.assertEqual(
                [i["instance_id"] for i in r3["instances"]], ["c2[1]"]
            )
            self.assertEqual(
                submit(
                    server,
                    "r3",
                    "c2[1]",
                    outcome="passed",
                    coverage={
                        "files": {
                            "a.py": {"executable_lines": [9], "covered_lines": []}
                        }
                    },
                )[0],
                200,
            )
            server.json("POST", "/v1/runs/r3/complete")
            status, coverage = server.json("GET", "/v1/runs/r3/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(
                coverage["files"],
                [
                    {
                        "path": "a.py",
                        "executable_lines": [9],
                        "covered_lines": [],
                        "missed_lines": [9],
                        "coverage_percent": 0.0,
                    }
                ],
            )
            self.assertEqual(coverage["summary"]["files"], 1)
            # Ancestor runs were not mutated by the downstream fragment.
            status, source = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(source["files"][0]["executable_lines"], [1, 2, 3])
            status, middle = server.json("GET", "/v1/runs/r2/coverage")
            self.assertEqual([f["path"] for f in middle["files"]], ["b.py"])

    def test_junit_export_unaffected_by_coverage(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(
                server,
                "r1",
                "c1[0]",
                coverage={
                    "files": {"a.py": {"executable_lines": [1], "covered_lines": [1]}}
                },
            )
            server.json("POST", "/v1/runs/r1/complete")
            status, content_type, body = server.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/xml; charset=utf-8")
            text = body.decode("utf-8")
            self.assertIn('<?xml version="1.0" encoding="UTF-8"?>', text)
            self.assertIn("<testsuite", text)
            self.assertIn('name="c1[0]"', text)
            self.assertNotIn("coverage", text)


if __name__ == "__main__":
    unittest.main()
