"""End-to-end HTTP tests for run archive export and import."""

from __future__ import annotations

import copy
import json
import unittest

from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases


def submit(server: ServerHarness, run_id: str, instance_id: str, outcome: str, **extra: object) -> tuple[int, dict]:
    payload = {"instance_id": instance_id, "outcome": outcome, "duration_ms": 5}
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def seed_completed_run(server: ServerHarness, run_id: str = "r1") -> dict:
    """A completed run with coverage, a failure and a skipped instance."""
    seed_cases(server)
    server.json("POST", "/v1/suites", {"id": "s2", "name": "S2"})
    server.json(
        "POST",
        "/v1/fixtures",
        {
            "id": "f1",
            "name": "F1",
            "setup_steps": [{"action": "setup"}],
            "teardown_steps": [{"action": "teardown"}],
        },
    )
    server.json(
        "POST",
        "/v1/cases",
        case_payload(
            "c3",
            suite_id="s2",
            kind="api",
            timeout_seconds=42,
            fixture_ids=["f1"],
            parameterization={"rows": [{"p": "a"}, {"p": "b"}]},
        ),
    )
    status, _ = server.json("POST", "/v1/runs", {"id": run_id, "case_ids": ["c3", "c1"]})
    assert status == 201
    status, _ = submit(
        server,
        run_id,
        "c3[0]",
        "passed",
        details={"note": "ok"},
        coverage={
            "files": {
                "src/b.py": {"executable_lines": [3, 1, 2], "covered_lines": [2, 1]},
                "src/a.py": {"executable_lines": [10], "covered_lines": []},
            }
        },
    )
    assert status == 200
    status, _ = submit(
        server,
        run_id,
        "c3[1]",
        "failed",
        details={"why": "boom"},
        coverage={"files": {"src/b.py": {"executable_lines": [1, 2, 4], "covered_lines": [4]}}},
    )
    assert status == 200
    status, _ = submit(server, run_id, "c1[0]", "skipped")
    assert status == 200
    status, report = server.json("POST", f"/v1/runs/{run_id}/complete")
    assert status == 200
    return report


class ArchiveExportTest(unittest.TestCase):
    def test_export_contains_report_contexts_and_coverage(self) -> None:
        with ServerHarness() as server:
            report = seed_completed_run(server)

            status, archive = server.json("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)
            self.assertEqual(set(archive), {"archive_version", "run", "contexts", "coverage"})
            self.assertEqual(archive["archive_version"], 1)
            self.assertEqual(archive["run"], report)
            self.assertEqual(archive["run"]["status"], "completed")

            contexts = archive["contexts"]
            self.assertEqual(len(contexts), len(report["instances"]))
            first = contexts[0]
            self.assertEqual(
                set(first),
                {
                    "case_name",
                    "kind",
                    "parameters",
                    "timeout_seconds",
                    "setup",
                    "steps",
                    "teardown",
                    "resource_requirements",
                },
            )
            self.assertEqual(first["case_name"], "name-c3")
            self.assertEqual(first["kind"], "api")
            self.assertEqual(first["parameters"], {"p": "a"})
            self.assertEqual(first["timeout_seconds"], 42)
            self.assertEqual(first["setup"], [{"fixture_id": "f1", "steps": [{"action": "setup"}]}])
            self.assertEqual(first["teardown"], [{"fixture_id": "f1", "steps": [{"action": "teardown"}]}])
            self.assertEqual(first["steps"], [{"action": "run"}])
            self.assertEqual(first["resource_requirements"], {})
            self.assertEqual(contexts[2]["case_name"], "name-c1")
            self.assertEqual(contexts[2]["setup"], [])
            self.assertEqual(contexts[2]["teardown"], [])

            # Merged, normalized coverage: sorted paths and line numbers.
            self.assertEqual(
                archive["coverage"],
                {
                    "files": {
                        "src/a.py": {"executable_lines": [10], "covered_lines": []},
                        "src/b.py": {"executable_lines": [1, 2, 3, 4], "covered_lines": [1, 2, 4]},
                    }
                },
            )

    def test_export_is_byte_identical_across_reads(self) -> None:
        with ServerHarness() as server:
            seed_completed_run(server)
            status, content_type, first = server.request("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json; charset=utf-8")
            status, _, second = server.request("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)
            self.assertEqual(first, second)

    def test_export_empty_coverage(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(server, "r1", "c1[0]", "passed")
            server.json("POST", "/v1/runs/r1/complete")
            status, archive = server.json("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)
            self.assertEqual(archive["coverage"], {"files": {}})

    def test_export_errors(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            status, body = server.json("GET", "/v1/runs/ghost/archive")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            server.json("POST", "/v1/runs", {"id": "open", "case_ids": ["c1"]})
            status, body = server.json("GET", "/v1/runs/open/archive")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")


class ArchiveImportTest(unittest.TestCase):
    def export(self, server: ServerHarness, run_id: str = "r1") -> bytes:
        status, _, body = server.request("GET", f"/v1/runs/{run_id}/archive")
        assert status == 200
        return body

    def test_round_trip_restores_identical_run(self) -> None:
        with ServerHarness() as source:
            report = seed_completed_run(source)
            archive_body = self.export(source)
            archive = json.loads(archive_body)
            junit_status, _, junit_body = source.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(junit_status, 200)
            _, coverage_before = source.json("GET", "/v1/runs/r1/coverage")
            _, diagnostics_before = source.json("GET", "/v1/runs/r1/diagnostics")

        # Restore into a fresh service with an empty catalog.
        with ServerHarness(Service()) as restored:
            status, imported = restored.json(
                "POST", "/v1/run-archives", raw_body=archive_body
            )
            self.assertEqual(status, 201)
            self.assertEqual(imported, report)

            status, fetched = restored.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(fetched, report)

            # Re-export is byte-identical to the source archive.
            status, _, reexported = restored.request("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 200)
            self.assertEqual(reexported, archive_body)

            # JUnit XML, coverage and diagnostics match the original run.
            status, _, junit_after = restored.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(status, 200)
            self.assertEqual(junit_after, junit_body)
            status, coverage_after = restored.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(coverage_after, coverage_before)
            status, diagnostics_after = restored.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(diagnostics_after, diagnostics_before)

            # Cross-run aggregation includes the restored run.
            status, aggregate = restored.json(
                "POST", "/v1/reports/aggregate", {"run_ids": ["r1"]}
            )
            self.assertEqual(status, 200)
            self.assertEqual(aggregate["run_count"], 1)
            self.assertEqual(aggregate["summary"]["failed"], 1)
            self.assertEqual(aggregate["summary"]["skipped"], 1)

            # The restored run is completed: claim, submit, timeouts and
            # complete all follow run_completed semantics.
            for path, body in (
                ("/v1/runs/r1/claims", {"worker_id": "w", "lease_seconds": 10}),
                (
                    "/v1/runs/r1/results",
                    {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 1},
                ),
                ("/v1/runs/r1/timeouts", {}),
            ):
                status, reply = restored.json("POST", path, body)
                self.assertEqual(status, 409, path)
                self.assertEqual(reply["error"]["code"], "run_completed", path)
            status, reply = restored.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 409)
            self.assertEqual(reply["error"]["code"], "run_completed")

            # Failure retry works on the restored run.
            status, retry = restored.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            self.assertEqual(retry["retry_of"], "r1")
            self.assertEqual(retry["root_run_id"], "r1")
            self.assertEqual(retry["attempt"], 1)
            self.assertEqual(
                [i["instance_id"] for i in retry["instances"]], ["c3[1]"]
            )

    def test_retry_archive_restores_lineage_without_ancestors(self) -> None:
        with ServerHarness() as source:
            seed_cases(source)
            source.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            submit(source, "r1", "c2[0]", "failed")
            submit(source, "r1", "c2[1]", "error")
            source.json("POST", "/v1/runs/r1/complete")
            source.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            submit(source, "r2", "c2[0]", "passed")
            submit(source, "r2", "c2[1]", "failed")
            source.json("POST", "/v1/runs/r2/complete")
            source.json("POST", "/v1/runs/r2/retry", {"id": "r3"})
            submit(source, "r3", "c2[1]", "error", details={"why": "still"})
            source.json("POST", "/v1/runs/r3/complete")
            archive_body = self.export(source, "r3")

        with ServerHarness(Service()) as restored:
            status, report = restored.json(
                "POST", "/v1/run-archives", raw_body=archive_body
            )
            self.assertEqual(status, 201)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["retry_of"], "r2")
            self.assertEqual(report["root_run_id"], "r1")
            self.assertEqual(report["attempt"], 2)
            # Ancestors were never imported and stay unknown.
            for ancestor in ("r1", "r2"):
                status, _ = restored.json("GET", f"/v1/runs/{ancestor}")
                self.assertEqual(status, 404)
            # Re-export stays byte-identical.
            status, _, reexported = restored.request("GET", "/v1/runs/r3/archive")
            self.assertEqual(status, 200)
            self.assertEqual(reexported, archive_body)
            # Diagnostics keep the frozen lineage.
            status, diagnostics = restored.json("GET", "/v1/runs/r3/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(diagnostics["retry_of"], "r2")
            self.assertEqual(diagnostics["root_run_id"], "r1")
            self.assertEqual(diagnostics["attempt"], 2)
            # Chained retry continues the lineage.
            status, r4 = restored.json("POST", "/v1/runs/r3/retry", {"id": "r4"})
            self.assertEqual(status, 201)
            self.assertEqual(r4["retry_of"], "r3")
            self.assertEqual(r4["root_run_id"], "r1")
            self.assertEqual(r4["attempt"], 3)

    def test_import_conflicts_with_existing_run_id(self) -> None:
        with ServerHarness() as server:
            seed_completed_run(server)
            archive_body = self.export(server)
            status, body = server.json("POST", "/v1/run-archives", raw_body=archive_body)
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_exists")

            # An open run with the same id also conflicts.
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "open", "case_ids": ["c1"]})
            archive = json.loads(archive_body)
            archive["run"]["id"] = "open"
            status, body = server.json("POST", "/v1/run-archives", archive)
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_exists")

    def test_import_malformed_json(self) -> None:
        with ServerHarness() as server:
            status, body = server.json("POST", "/v1/run-archives", raw_body=b"{not json")
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

    def test_import_validation_errors_leave_no_state(self) -> None:
        with ServerHarness() as source:
            seed_completed_run(source)
            archive = json.loads(self.export(source))

        def mutated(**changes: object) -> dict:
            candidate = copy.deepcopy(archive)
            for key, value in changes.items():
                if value is ...:
                    candidate.pop(key)
                else:
                    candidate[key] = value
            return candidate

        bad_payloads: list[object] = [
            [],
            "archive",
            123,
            None,
            {},
            mutated(archive_version=...),
            mutated(run=...),
            mutated(contexts=...),
            mutated(coverage=...),
            mutated(extra={}),
            mutated(archive_version=2),
            mutated(archive_version="1"),
            mutated(archive_version=True),
            mutated(archive_version=1.0),
            mutated(run=None),
        ]
        # run-level problems
        for run_change in (
            {"status": "open"},
            {"passed": "yes"},
            {"passed": True},  # summary has failures, so passed must be false
            {"id": ""},
            {"id": 7},
            {"summary": {"total": 3, "pending": 0, "passed": 1, "failed": 1, "error": 0, "skipped": 1, "duration_ms": 15, "extra": 1}},
            {"retry_of": "r0"},  # partial retry metadata
            {"attempt": 0, "retry_of": "r0", "root_run_id": "r0"},
        ):
            candidate = copy.deepcopy(archive)
            candidate["run"].update(run_change)
            bad_payloads.append(candidate)
        for key in ("id", "status", "passed", "instances", "summary"):
            candidate = copy.deepcopy(archive)
            del candidate["run"][key]
            bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["run"]["unknown"] = 1
        bad_payloads.append(candidate)
        # summary not recomputable from results
        candidate = copy.deepcopy(archive)
        candidate["run"]["summary"]["failed"] = 0
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["run"]["summary"]["duration_ms"] = 999
        bad_payloads.append(candidate)
        # instance-level problems
        candidate = copy.deepcopy(archive)
        candidate["run"]["instances"][0]["outcome"] = "pending"
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["run"]["instances"][1]["instance_id"] = candidate["run"]["instances"][0]["instance_id"]
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["run"]["instances"][0]["lease"] = {"claim_id": "x"}
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        del candidate["run"]["instances"][0]["duration_ms"]
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["run"]["instances"][0]["duration_ms"] = -1
        bad_payloads.append(candidate)
        # context correspondence problems
        candidate = copy.deepcopy(archive)
        candidate["contexts"] = candidate["contexts"][:-1]
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["contexts"][0]["parameters"] = {"p": "zzz"}
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["contexts"][0]["case_name"] = "other"
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["contexts"][0]["kind"] = "bogus"
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        del candidate["contexts"][0]["setup"]
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["contexts"][0]["resource_requirements"] = {"pool": 0}
        bad_payloads.append(candidate)
        # coverage problems
        candidate = copy.deepcopy(archive)
        candidate["coverage"] = {"files": {}, "extra": 1}
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["coverage"]["files"]["src/b.py"]["covered_lines"] = [99]
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["coverage"]["files"]["src/b.py"]["executable_lines"] = [2, 1, 3, 4]
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["coverage"]["files"]["src/b.py"]["covered_lines"] = [1, 1]
        bad_payloads.append(candidate)
        candidate = copy.deepcopy(archive)
        candidate["coverage"]["files"][""] = {"executable_lines": [1], "covered_lines": []}
        bad_payloads.append(candidate)

        with ServerHarness(Service()) as server:
            for payload in bad_payloads:
                status, body = server.json("POST", "/v1/run-archives", payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(body["error"]["code"], "validation_error", payload)
                # No partial run or coverage state, and the id stays free.
                status, _ = server.json("GET", "/v1/runs/r1")
                self.assertEqual(status, 404, payload)
                status, _ = server.json("GET", "/v1/runs/r1/coverage")
                self.assertEqual(status, 404, payload)

            # The id is still usable afterwards, for import and normal runs.
            status, report = server.json("POST", "/v1/run-archives", archive)
            self.assertEqual(status, 201)
            self.assertEqual(report["id"], "r1")

    def test_imported_archive_is_isolated_from_source_payload(self) -> None:
        with ServerHarness() as source:
            seed_completed_run(source)
            archive = json.loads(self.export(source))

        with ServerHarness(Service()) as server:
            status, _ = server.json("POST", "/v1/run-archives", archive)
            self.assertEqual(status, 201)
            # Mutating the submitted object must not corrupt stored state.
            archive["run"]["instances"][0]["outcome"] = "error"
            archive["contexts"][0]["steps"].append({"action": "extra"})
            archive["coverage"]["files"]["src/a.py"]["covered_lines"].append(10)
            status, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(report["instances"][0]["outcome"], "passed")
            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            a_file = next(f for f in coverage["files"] if f["path"] == "src/a.py")
            self.assertEqual(a_file["covered_lines"], [])


if __name__ == "__main__":
    unittest.main()
