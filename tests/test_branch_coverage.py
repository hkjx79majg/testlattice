"""End-to-end HTTP tests for optional branch coverage: paired branch
coordinate arrays on coverage fragments, merged branch statistics on the
coverage report, archive round-trips and the CI branch-coverage gate."""

from __future__ import annotations

import unittest

import testlattice.service as service_module
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


def evaluate(server: ServerHarness, run_ids: list[str], policy: dict) -> tuple[int, dict]:
    return server.json(
        "POST", "/v1/ci/gates/evaluate", {"run_ids": run_ids, "policy": policy}
    )


LINES = {"executable_lines": [1, 2], "covered_lines": [1]}
BRANCHES_AB = [
    {"line": 1, "branch": 0},
    {"line": 1, "branch": 1},
    {"line": 2, "branch": 0},
]


class BranchCoverageFragmentTest(unittest.TestCase):
    def test_merges_branch_coordinates_with_union_and_ordering(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})

            status, _ = submit(
                server,
                "r1",
                "c2[0]",
                coverage={
                    "files": {
                        # Request coordinate order is unsorted; the report sorts.
                        "a.py": {
                            **LINES,
                            "executable_branches": [BRANCHES_AB[2], BRANCHES_AB[0]],
                            "covered_branches": [BRANCHES_AB[0]],
                        },
                        # Line-only files coexist with branch-carrying files.
                        "b.py": {"executable_lines": [5], "covered_lines": []},
                    }
                },
            )
            self.assertEqual(status, 200)
            status, _ = submit(
                server,
                "r1",
                "c2[1]",
                coverage={
                    "files": {
                        "a.py": {
                            **LINES,
                            "executable_branches": [BRANCHES_AB[0], BRANCHES_AB[1]],
                            "covered_branches": [BRANCHES_AB[1]],
                        }
                    }
                },
            )
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/r1/complete")

            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            by_path = {entry["path"]: entry for entry in coverage["files"]}
            self.assertEqual(
                by_path["a.py"],
                {
                    "path": "a.py",
                    "executable_lines": [1, 2],
                    "covered_lines": [1],
                    "missed_lines": [2],
                    "coverage_percent": 50.0,
                    "executable_branches": BRANCHES_AB,
                    "covered_branches": [BRANCHES_AB[0], BRANCHES_AB[1]],
                    "missed_branches": [BRANCHES_AB[2]],
                    "branches": {
                        "executable": 3,
                        "covered": 2,
                        "missed": 1,
                        "coverage_percent": 66.67,
                    },
                },
            )
            # The line-only file gains no branch fields.
            self.assertEqual(
                by_path["b.py"],
                {
                    "path": "b.py",
                    "executable_lines": [5],
                    "covered_lines": [],
                    "missed_lines": [5],
                    "coverage_percent": 0.0,
                },
            )
            self.assertEqual(
                coverage["summary"],
                {
                    "files": 2,
                    "executable_lines": 3,
                    "covered_lines": 1,
                    "missed_lines": 2,
                    "coverage_percent": 33.33,
                    "branches": {
                        "executable": 3,
                        "covered": 2,
                        "missed": 1,
                        "coverage_percent": 66.67,
                    },
                },
            )

    def test_run_without_branch_info_keeps_line_only_shape(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, _ = submit(server, "r1", "c1[0]", coverage={"files": {"a.py": LINES}})
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/r1/complete")
            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(
                set(coverage["summary"]),
                {"files", "executable_lines", "covered_lines", "missed_lines", "coverage_percent"},
            )
            self.assertEqual(
                set(coverage["files"][0]),
                {"path", "executable_lines", "covered_lines", "missed_lines", "coverage_percent"},
            )

    def test_invalid_branch_fragments_rejected_atomically(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            coord = {"line": 1, "branch": 0}
            bad_fragments = [
                # Branch fields must be paired.
                {"files": {"a.py": {**LINES, "executable_branches": [coord]}}},
                {"files": {"a.py": {**LINES, "covered_branches": []}}},
                # Executable branches must not be empty.
                {"files": {"a.py": {**LINES, "executable_branches": [], "covered_branches": []}}},
                # No duplicate coordinates.
                {"files": {"a.py": {**LINES, "executable_branches": [coord, coord], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [coord], "covered_branches": [coord, coord]}}},
                # Covered branches must be a subset.
                {"files": {"a.py": {**LINES, "executable_branches": [coord], "covered_branches": [{"line": 2, "branch": 0}]}}},
                {"files": {"a.py": {**LINES, "executable_branches": [coord], "covered_branches": [{"line": 1, "branch": 1}]}}},
                # Coordinate element types and ranges.
                {"files": {"a.py": {**LINES, "executable_branches": "x", "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [1], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"line": 1}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"branch": 0}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"line": 0, "branch": 0}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"line": -1, "branch": 0}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"line": 1.5, "branch": 0}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"line": True, "branch": 0}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"line": 1, "branch": -1}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"line": 1, "branch": 0.5}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"line": 1, "branch": True}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": [{"line": 1, "branch": 0, "x": 1}], "covered_branches": []}}},
                {"files": {"a.py": {**LINES, "executable_branches": None, "covered_branches": []}}},
            ]
            for fragment in bad_fragments:
                status, body = submit(server, "r1", "c2[0]", coverage=fragment)
                self.assertEqual(status, 400, fragment)
                self.assertEqual(body["error"]["code"], "validation_error", fragment)

            # Nothing was saved: both instances stay pending.
            status, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["pending"], 2)

            # A valid fragment for the same instance is still accepted.
            status, _ = submit(
                server,
                "r1",
                "c2[0]",
                coverage={
                    "files": {
                        "a.py": {
                            **LINES,
                            "executable_branches": [coord],
                            "covered_branches": [],
                        }
                    }
                },
            )
            self.assertEqual(status, 200)
            status, _ = submit(server, "r1", "c2[1]")
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/r1/complete")
            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(
                coverage["summary"]["branches"],
                {"executable": 1, "covered": 0, "missed": 1, "coverage_percent": 0.0},
            )

    def test_retry_accumulates_only_its_own_branch_fragments(self) -> None:
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
                        "a.py": {
                            **LINES,
                            "executable_branches": BRANCHES_AB,
                            "covered_branches": BRANCHES_AB[:1],
                        }
                    }
                },
            )
            server.json("POST", "/v1/runs/r1/complete")
            status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            submit(server, "r2", "c1[0]", outcome="passed")
            server.json("POST", "/v1/runs/r2/complete")

            # The retry submitted no fragments: no branch info at all.
            status, coverage = server.json("GET", "/v1/runs/r2/coverage")
            self.assertEqual(status, 200)
            self.assertNotIn("branches", coverage["summary"])
            self.assertEqual(coverage["files"], [])

            # The source run keeps its own branch data.
            status, source = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(source["summary"]["branches"]["executable"], 3)


class BranchCoverageArchiveTest(unittest.TestCase):
    def test_archive_roundtrip_preserves_branch_coverage(self) -> None:
        with ServerHarness() as source:
            seed_cases(source)
            source.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            submit(
                source,
                "r1",
                "c2[0]",
                coverage={
                    "files": {
                        "a.py": {
                            **LINES,
                            "executable_branches": BRANCHES_AB,
                            "covered_branches": BRANCHES_AB[:2],
                        },
                        "b.py": {"executable_lines": [9], "covered_lines": []},
                    }
                },
            )
            submit(source, "r1", "c2[1]")
            source.json("POST", "/v1/runs/r1/complete")
            _, before = source.json("GET", "/v1/runs/r1/coverage")
            status, archive = source.json("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)
            by_path = {
                entry["path"]: entry for entry in archive["coverage"]["files"]
            }
            self.assertEqual(by_path["a.py"]["executable_branches"], BRANCHES_AB)
            self.assertEqual(
                by_path["a.py"]["covered_branches"], BRANCHES_AB[:2]
            )
            self.assertNotIn("executable_branches", by_path["b.py"])

        with ServerHarness(service_module.Service()) as target:
            status, body = target.json("POST", "/v1/run-archives", archive)
            self.assertEqual(status, 201, body)
            status, after = target.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(after, before)
            # Re-exporting the restored run reproduces the same archive.
            status, reexported = target.json("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)
            self.assertEqual(reexported, archive)

    def test_line_only_archive_still_imports(self) -> None:
        with ServerHarness() as source:
            seed_cases(source)
            source.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(source, "r1", "c1[0]", coverage={"files": {"a.py": LINES}})
            source.json("POST", "/v1/runs/r1/complete")
            _, archive = source.json("GET", "/v1/runs/r1/archive")
            _, before = source.json("GET", "/v1/runs/r1/coverage")

        with ServerHarness(service_module.Service()) as target:
            status, body = target.json("POST", "/v1/run-archives", archive)
            self.assertEqual(status, 201, body)
            _, after = target.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(after, before)
            self.assertNotIn("branches", after["summary"])

    def test_archive_branch_validation_failures(self) -> None:
        with ServerHarness() as source:
            seed_cases(source)
            source.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(
                source,
                "r1",
                "c1[0]",
                coverage={
                    "files": {
                        "a.py": {
                            **LINES,
                            "executable_branches": BRANCHES_AB,
                            "covered_branches": BRANCHES_AB[:1],
                        }
                    }
                },
            )
            source.json("POST", "/v1/runs/r1/complete")
            _, archive = source.json("GET", "/v1/runs/r1/archive")

        import copy

        def import_mutated(mutate: object) -> tuple[int, dict]:
            mutated = copy.deepcopy(archive)
            mutate(mutated["coverage"]["files"][0])
            with ServerHarness(service_module.Service()) as target:
                return target.json("POST", "/v1/run-archives", mutated)

        bad_mutations = [
            # Branch fields must stay paired.
            lambda f: f.pop("executable_branches"),
            lambda f: f.pop("covered_branches"),
            # Executable branches must not be empty.
            lambda f: f.update(executable_branches=[], covered_branches=[]),
            # Unique ascending order is required in archives.
            lambda f: f.update(
                executable_branches=[BRANCHES_AB[1], BRANCHES_AB[0]],
                covered_branches=[],
            ),
            lambda f: f.update(
                executable_branches=[BRANCHES_AB[0], BRANCHES_AB[0]],
                covered_branches=[],
            ),
            # Covered branches must be a subset.
            lambda f: f.update(covered_branches=[{"line": 9, "branch": 0}]),
            # Coordinate shape is enforced.
            lambda f: f.update(executable_branches=[{"line": 1, "branch": 0, "x": 1}]),
            lambda f: f.update(executable_branches=[{"line": 1, "branch": -1}]),
        ]
        for mutate in bad_mutations:
            status, body = import_mutated(mutate)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")


class BranchCoverageGateTest(unittest.TestCase):
    def _run_with_branches(
        self, server: ServerHarness, run_id: str, covered: int, executable: int
    ) -> None:
        server.json("POST", "/v1/runs", {"id": run_id, "case_ids": ["c1"]})
        coords = [{"line": 1, "branch": index} for index in range(executable)]
        status, _ = submit(
            server,
            run_id,
            "c1[0]",
            coverage={
                "files": {
                    "a.py": {
                        **LINES,
                        "executable_branches": coords,
                        "covered_branches": coords[:covered],
                    }
                }
            },
        )
        self.assertEqual(status, 200)
        status, _ = server.json("POST", f"/v1/runs/{run_id}/complete")
        self.assertEqual(status, 200)

    def test_branch_gate_uses_current_run_merged_percent(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            self._run_with_branches(server, "r1", covered=2, executable=3)

            status, report = evaluate(
                server, ["r1"], {"min_branch_coverage_percent": 66.67}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                report["checks"],
                [
                    {
                        "name": "branch_coverage",
                        "passed": True,
                        "threshold": 66.67,
                        "actual": 66.67,
                    }
                ],
            )
            self.assertIs(report["passed"], True)

            status, report = evaluate(
                server, ["r1"], {"min_branch_coverage_percent": 66.68}
            )
            self.assertEqual(report["checks"][0]["passed"], False)
            self.assertIs(report["passed"], False)

            # Full branch coverage against the maximum threshold passes.
            self._run_with_branches(server, "r2", covered=2, executable=2)
            status, report = evaluate(
                server, ["r2"], {"min_branch_coverage_percent": 100}
            )
            self.assertEqual(report["checks"][0]["actual"], 100.0)
            self.assertIs(report["passed"], True)

    def test_branch_gate_unavailable_without_branch_info(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(server, "r1", "c1[0]", coverage={"files": {"a.py": LINES}})
            server.json("POST", "/v1/runs/r1/complete")

            # Even a zero threshold cannot pass when branch data is missing.
            status, report = evaluate(
                server, ["r1"], {"min_branch_coverage_percent": 0}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                report["checks"],
                [
                    {
                        "name": "branch_coverage",
                        "passed": False,
                        "threshold": 0,
                        "actual": None,
                        "code": "branch_coverage_unavailable",
                    }
                ],
            )
            self.assertIs(report["passed"], False)

    def test_branch_gate_absent_by_default_and_check_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            self._run_with_branches(server, "r1", covered=1, executable=1)

            # Without the policy field there is no branch check.
            status, report = evaluate(server, ["r1"], {"min_coverage_percent": 50})
            self.assertEqual(status, 200)
            self.assertEqual(
                [c["name"] for c in report["checks"]], ["min_coverage_percent"]
            )

            # Declaration order is ignored; branch_coverage sorts between
            # min_coverage_percent and max_flaky.
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
            submit(server, "r2", "c1[0]")
            server.json("POST", "/v1/runs/r2/complete")
            status, report = evaluate(
                server,
                ["r1", "r2"],
                {
                    "max_flaky": 1,
                    "min_branch_coverage_percent": 100,
                    "min_coverage_percent": 0,
                    "max_error": 0,
                    "max_failed": 0,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                [c["name"] for c in report["checks"]],
                [
                    "max_failed",
                    "max_error",
                    "min_coverage_percent",
                    "branch_coverage",
                    "max_flaky",
                ],
            )

    def test_branch_gate_policy_validation(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(server, "r1", "c1[0]")
            server.json("POST", "/v1/runs/r1/complete")

            bad_values = [-0.01, 100.01, True, False, "80", None, [1]]
            for value in bad_values:
                status, body = evaluate(
                    server, ["r1"], {"min_branch_coverage_percent": value}
                )
                self.assertEqual(status, 400, value)
                self.assertEqual(body["error"]["code"], "validation_error", value)
            for non_finite in (float("nan"), float("inf"), -float("inf")):
                status, body = evaluate(
                    server, ["r1"], {"min_branch_coverage_percent": non_finite}
                )
                self.assertEqual(status, 400, non_finite)
                self.assertEqual(body["error"]["code"], "validation_error")

            # Boundary values are accepted.
            for value in (0, 100, 50.5):
                status, _ = evaluate(
                    server, ["r1"], {"min_branch_coverage_percent": value}
                )
                self.assertEqual(status, 200, value)


if __name__ == "__main__":
    unittest.main()
