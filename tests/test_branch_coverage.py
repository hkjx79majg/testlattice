"""End-to-end HTTP tests for optional branch coverage: fragment validation,
merged branch reporting, archives and the CI branch-coverage gate."""

from __future__ import annotations

import json
import math
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


BRANCH_FRAGMENT_A = {
    "files": {
        "a.py": {
            "executable_lines": [1, 2, 3],
            "covered_lines": [1, 2],
            # Request order is unsorted; the report must sort by (line, branch).
            "executable_branches": [
                {"line": 3, "branch": 1},
                {"line": 1, "branch": 0},
                {"line": 1, "branch": 1},
            ],
            "covered_branches": [{"line": 1, "branch": 0}],
        }
    }
}
BRANCH_FRAGMENT_B = {
    "files": {
        "a.py": {
            "executable_lines": [3, 4],
            "covered_lines": [3],
            "executable_branches": [{"line": 3, "branch": 1}, {"line": 4, "branch": 0}],
            "covered_branches": [{"line": 4, "branch": 0}, {"line": 3, "branch": 1}],
        }
    }
}


class BranchCoverageReportTest(unittest.TestCase):
    def test_merges_branch_coordinates_per_file(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            status, _ = submit(server, "r1", "c2[0]", coverage=BRANCH_FRAGMENT_A)
            self.assertEqual(status, 200)
            status, _ = submit(server, "r1", "c2[1]", coverage=BRANCH_FRAGMENT_B)
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/r1/complete")

            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(
                coverage["files"],
                [
                    {
                        "path": "a.py",
                        "executable_lines": [1, 2, 3, 4],
                        "covered_lines": [1, 2, 3],
                        "missed_lines": [4],
                        "coverage_percent": 75.0,
                        "branches": {
                            "executable": 4,
                            "covered": 3,
                            "missed": 1,
                            "coverage_percent": 75.0,
                        },
                        "missed_branches": [
                            {"line": 1, "branch": 1},
                        ],
                    }
                ],
            )
            self.assertEqual(
                coverage["summary"],
                {
                    "files": 1,
                    "executable_lines": 4,
                    "covered_lines": 3,
                    "missed_lines": 1,
                    "coverage_percent": 75.0,
                    "branches": {
                        "executable": 4,
                        "covered": 3,
                        "missed": 1,
                        "coverage_percent": 75.0,
                    },
                },
            )

            # Repeated reads are stable and the run report stays line-free.
            _, _, raw_a = server.request("GET", "/v1/runs/r1/coverage")
            _, _, raw_b = server.request("GET", "/v1/runs/r1/coverage")
            self.assertEqual(raw_a, raw_b)
            _, report = server.json("GET", "/v1/runs/r1")
            for instance in report["instances"]:
                self.assertNotIn("coverage", instance)

    def test_mixed_files_only_branch_files_get_fields(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            submit(
                server,
                "r1",
                "c2[0]",
                coverage={
                    "files": {
                        "with.py": {
                            "executable_lines": [1],
                            "covered_lines": [1],
                            "executable_branches": [{"line": 1, "branch": 0}],
                            "covered_branches": [],
                        },
                        "without.py": {
                            "executable_lines": [5],
                            "covered_lines": [5],
                        },
                    }
                },
            )
            submit(server, "r1", "c2[1]")
            server.json("POST", "/v1/runs/r1/complete")
            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            by_path = {entry["path"]: entry for entry in coverage["files"]}
            self.assertEqual(
                by_path["with.py"]["branches"],
                {"executable": 1, "covered": 0, "missed": 1, "coverage_percent": 0.0},
            )
            self.assertEqual(
                by_path["with.py"]["missed_branches"], [{"line": 1, "branch": 0}]
            )
            self.assertNotIn("branches", by_path["without.py"])
            self.assertNotIn("missed_branches", by_path["without.py"])
            # The run has branch information, so the summary totals appear.
            self.assertEqual(
                coverage["summary"]["branches"],
                {"executable": 1, "covered": 0, "missed": 1, "coverage_percent": 0.0},
            )

    def test_no_branch_info_anywhere_keeps_line_only_shape(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(
                server,
                "r1",
                "c1[0]",
                coverage={
                    "files": {
                        "a.py": {"executable_lines": [1, 2], "covered_lines": [1]}
                    }
                },
            )
            server.json("POST", "/v1/runs/r1/complete")
            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(
                coverage,
                {
                    "run_id": "r1",
                    "summary": {
                        "files": 1,
                        "executable_lines": 2,
                        "covered_lines": 1,
                        "missed_lines": 1,
                        "coverage_percent": 50.0,
                    },
                    "files": [
                        {
                            "path": "a.py",
                            "executable_lines": [1, 2],
                            "covered_lines": [1],
                            "missed_lines": [2],
                            "coverage_percent": 50.0,
                        }
                    ],
                },
            )

    def test_branch_rounding_half_up(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(
                server,
                "r1",
                "c1[0]",
                coverage={
                    "files": {
                        "a.py": {
                            "executable_lines": [1],
                            "covered_lines": [1],
                            "executable_branches": [
                                {"line": 1, "branch": 0},
                                {"line": 1, "branch": 1},
                                {"line": 1, "branch": 2},
                            ],
                            "covered_branches": [{"line": 1, "branch": 0}],
                        }
                    }
                },
            )
            server.json("POST", "/v1/runs/r1/complete")
            _, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(coverage["files"][0]["branches"]["coverage_percent"], 33.33)
            self.assertEqual(coverage["summary"]["branches"]["coverage_percent"], 33.33)


class BranchCoverageValidationTest(unittest.TestCase):
    def test_invalid_branch_fragments_rejected_atomically(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            lines = {"executable_lines": [1], "covered_lines": [1]}
            exec_b = [{"line": 1, "branch": 0}]
            bad_files = [
                # Unpaired branch fields.
                {**lines, "executable_branches": exec_b},
                {**lines, "covered_branches": []},
                # Not arrays.
                {**lines, "executable_branches": "x", "covered_branches": []},
                {**lines, "executable_branches": None, "covered_branches": []},
                {**lines, "executable_branches": exec_b, "covered_branches": {}},
                # Empty executable set.
                {**lines, "executable_branches": [], "covered_branches": []},
                # Malformed elements.
                {**lines, "executable_branches": [1], "covered_branches": []},
                {**lines, "executable_branches": [None], "covered_branches": []},
                {**lines, "executable_branches": [{}], "covered_branches": []},
                {**lines, "executable_branches": [{"line": 1}], "covered_branches": []},
                {**lines, "executable_branches": [{"branch": 0}], "covered_branches": []},
                {
                    **lines,
                    "executable_branches": [{"line": 1, "branch": 0, "x": 1}],
                    "covered_branches": [],
                },
                # Bad coordinate values.
                {**lines, "executable_branches": [{"line": 0, "branch": 0}], "covered_branches": []},
                {**lines, "executable_branches": [{"line": -1, "branch": 0}], "covered_branches": []},
                {**lines, "executable_branches": [{"line": 1.5, "branch": 0}], "covered_branches": []},
                {**lines, "executable_branches": [{"line": True, "branch": 0}], "covered_branches": []},
                {**lines, "executable_branches": [{"line": "1", "branch": 0}], "covered_branches": []},
                {**lines, "executable_branches": [{"line": 1, "branch": -1}], "covered_branches": []},
                {**lines, "executable_branches": [{"line": 1, "branch": 0.5}], "covered_branches": []},
                {**lines, "executable_branches": [{"line": 1, "branch": False}], "covered_branches": []},
                # Duplicates in either set.
                {
                    **lines,
                    "executable_branches": [{"line": 1, "branch": 0}, {"line": 1, "branch": 0}],
                    "covered_branches": [],
                },
                {
                    **lines,
                    "executable_branches": [{"line": 1, "branch": 0}, {"line": 1, "branch": 1}],
                    "covered_branches": [{"line": 1, "branch": 0}, {"line": 1, "branch": 0}],
                },
                # Covered not a subset of executable.
                {
                    **lines,
                    "executable_branches": exec_b,
                    "covered_branches": [{"line": 1, "branch": 1}],
                },
                {
                    **lines,
                    "executable_branches": exec_b,
                    "covered_branches": [{"line": 2, "branch": 0}],
                },
            ]
            for files_entry in bad_files:
                fragment = {"files": {"a.py": files_entry}}
                status, body = submit(server, "r1", "c2[0]", coverage=fragment)
                self.assertEqual(status, 400, files_entry)
                self.assertEqual(body["error"]["code"], "validation_error", files_entry)

            # Nothing was written; both instances remain pending.
            _, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(report["summary"]["pending"], 2)

            # A valid branch fragment still lands cleanly afterwards.
            status, _ = submit(server, "r1", "c2[0]", coverage=BRANCH_FRAGMENT_A)
            self.assertEqual(status, 200)
            submit(server, "r1", "c2[1]")
            server.json("POST", "/v1/runs/r1/complete")
            _, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(coverage["summary"]["branches"]["executable"], 3)

    def test_line_only_clients_stay_compatible(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, _ = submit(
                server,
                "r1",
                "c1[0]",
                coverage={
                    "files": {"a.py": {"executable_lines": [1], "covered_lines": []}}
                },
            )
            self.assertEqual(status, 200)
            server.json("POST", "/v1/runs/r1/complete")
            _, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertNotIn("branches", coverage["summary"])
            self.assertNotIn("branches", coverage["files"][0])

    def test_retry_accumulates_only_own_branch_fragments(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(server, "r1", "c1[0]", outcome="failed", coverage=BRANCH_FRAGMENT_A)
            server.json("POST", "/v1/runs/r1/complete")
            status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            submit(server, "r2", "c1[0]", outcome="passed")
            server.json("POST", "/v1/runs/r2/complete")
            _, coverage = server.json("GET", "/v1/runs/r2/coverage")
            self.assertEqual(coverage["files"], [])
            self.assertNotIn("branches", coverage["summary"])
            # The source run kept its branch data.
            _, source = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(source["summary"]["branches"]["executable"], 3)
            self.assertEqual(source["summary"]["branches"]["covered"], 1)


class BranchCoverageArchiveTest(unittest.TestCase):
    def _build_branch_run(self, server: ServerHarness) -> None:
        seed_cases(server)
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
        submit(server, "r1", "c2[0]", coverage=BRANCH_FRAGMENT_A)
        submit(server, "r1", "c2[1]", coverage=BRANCH_FRAGMENT_B)
        server.json("POST", "/v1/runs/r1/complete")

    def test_archive_roundtrip_preserves_branch_coverage(self) -> None:
        with ServerHarness() as source:
            self._build_branch_run(source)
            status, _, raw = source.request("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)
            archive = json.loads(raw)
            entry = archive["coverage"]["files"][0]
            self.assertEqual(
                entry["executable_branches"],
                [
                    {"line": 1, "branch": 0},
                    {"line": 1, "branch": 1},
                    {"line": 3, "branch": 1},
                    {"line": 4, "branch": 0},
                ],
            )
            self.assertEqual(
                entry["covered_branches"],
                [{"line": 1, "branch": 0}, {"line": 3, "branch": 1}, {"line": 4, "branch": 0}],
            )
            _, coverage_before = source.json("GET", "/v1/runs/r1/coverage")

        with ServerHarness(service_module.Service()) as target:
            status, _ = target.json("POST", "/v1/run-archives", raw_body=raw)
            self.assertEqual(status, 201)
            _, coverage_after = target.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(coverage_after, coverage_before)
            # Re-exporting the restored run is byte-identical to the source.
            _, _, raw_again = target.request("GET", "/v1/runs/r1/archive")
            self.assertEqual(raw_again, raw)

    def test_archive_branch_validation(self) -> None:
        with ServerHarness() as source:
            self._build_branch_run(source)
            _, archive = source.json("GET", "/v1/runs/r1/archive")

        def mutated(fn: object) -> dict:
            candidate = json.loads(json.dumps(archive))
            fn(candidate["coverage"]["files"][0])
            return candidate

        bad_archives = [
            mutated(lambda f: f.pop("covered_branches")),
            mutated(lambda f: f.pop("executable_branches")),
            mutated(lambda f: f.update(executable_branches=[])),
            mutated(
                lambda f: f.update(
                    executable_branches=[{"line": 1, "branch": 0}, {"line": 1, "branch": 0}]
                )
            ),
            mutated(
                lambda f: f.update(
                    executable_branches=[{"line": 2, "branch": 0}, {"line": 1, "branch": 0}]
                )
            ),
            mutated(lambda f: f.update(executable_branches=[{"line": 1}])),
            mutated(lambda f: f.update(executable_branches=[{"line": 1, "branch": -1}])),
            mutated(
                lambda f: f.update(covered_branches=[{"line": 9, "branch": 0}])
            ),
        ]
        for candidate in bad_archives:
            with ServerHarness(service_module.Service()) as target:
                status, body = target.json("POST", "/v1/run-archives", candidate)
                self.assertEqual(status, 400, candidate["coverage"]["files"][0])
                self.assertEqual(body["error"]["code"], "validation_error")
                status, _ = target.json("GET", "/v1/runs/r1")
                self.assertEqual(status, 404)

    def test_legacy_archive_without_branches_still_imports(self) -> None:
        with ServerHarness() as source:
            self._build_branch_run(source)
            _, archive = source.json("GET", "/v1/runs/r1/archive")
        # Simulate an archive written before branch coverage existed.
        for entry in archive["coverage"]["files"]:
            entry.pop("executable_branches", None)
            entry.pop("covered_branches", None)
        with ServerHarness(service_module.Service()) as target:
            status, _ = target.json("POST", "/v1/run-archives", archive)
            self.assertEqual(status, 201)
            _, coverage = target.json("GET", "/v1/runs/r1/coverage")
            self.assertNotIn("branches", coverage["summary"])
            self.assertNotIn("branches", coverage["files"][0])
            self.assertEqual(coverage["files"][0]["executable_lines"], [1, 2, 3, 4])


class BranchCoverageGateTest(unittest.TestCase):
    def _completed_branch_run(self, server: ServerHarness, run_id: str) -> None:
        server.json("POST", "/v1/runs", {"id": run_id, "case_ids": ["c1"]})
        submit(server, run_id, "c1[0]", coverage=BRANCH_FRAGMENT_A)
        server.json("POST", f"/v1/runs/{run_id}/complete")

    def test_branch_gate_pass_fail_and_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            self._completed_branch_run(server, "r1")

            # Merged branch coverage is 1/3 -> 33.33.
            status, report = evaluate(
                server, ["r1"], {"min_branch_coverage_percent": 33.33}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                report["checks"],
                [
                    {
                        "name": "branch_coverage",
                        "passed": True,
                        "threshold": 33.33,
                        "actual": 33.33,
                    }
                ],
            )
            self.assertIs(report["passed"], True)

            status, report = evaluate(
                server, ["r1"], {"min_branch_coverage_percent": 33.34}
            )
            self.assertEqual(report["checks"][0]["passed"], False)
            self.assertIs(report["passed"], False)

            # The check slots in before max_flaky, after min_coverage_percent.
            self._completed_branch_run(server, "r2")
            status, report = evaluate(
                server,
                ["r1", "r2"],
                {
                    "max_flaky": 0,
                    "min_branch_coverage_percent": 10,
                    "min_coverage_percent": 10,
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

    def test_branch_gate_unavailable_without_branch_info(self) -> None:
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

            # Even a zero threshold fails when the run has no branch data.
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

    def test_branch_gate_policy_validation(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(server, "r1", "c1[0]")
            server.json("POST", "/v1/runs/r1/complete")

            bad_values = [
                True,
                False,
                "80",
                None,
                [1],
                -0.01,
                100.01,
                math.nan,
                math.inf,
                -math.inf,
            ]
            for value in bad_values:
                status, body = evaluate(
                    server, ["r1"], {"min_branch_coverage_percent": value}
                )
                self.assertEqual(status, 400, value)
                self.assertEqual(body["error"]["code"], "validation_error", value)

            # Boundary values are accepted.
            for value in (0, 100, 50.5):
                status, _ = evaluate(
                    server, ["r1"], {"min_branch_coverage_percent": value}
                )
                self.assertEqual(status, 200, value)

    def test_branch_data_stays_out_of_other_surfaces(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            self._completed_branch_run(server, "r1")

            _, report = server.json("GET", "/v1/runs/r1")
            self.assertNotIn("branches", json.dumps(report))

            _, _, junit = server.request("GET", "/v1/runs/r1/junit.xml")
            self.assertNotIn("branch", junit.decode("utf-8"))

            _, diagnostics = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertNotIn("branch", json.dumps(diagnostics))

            _, aggregate = server.json("POST", "/v1/reports/aggregate", {"run_ids": ["r1"]})
            self.assertNotIn("branch", json.dumps(aggregate))


if __name__ == "__main__":
    unittest.main()
