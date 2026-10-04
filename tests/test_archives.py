"""End-to-end HTTP tests for transferable run archives."""

from __future__ import annotations

import copy
import json
import unittest

from test_runs import ServerHarness, case_payload, seed_cases


def submit(
    server: ServerHarness,
    run_id: str,
    instance_id: str,
    outcome: str = "passed",
    duration_ms: int = 5,
    **extra: object,
) -> tuple[int, dict]:
    payload: dict = {
        "instance_id": instance_id,
        "outcome": outcome,
        "duration_ms": duration_ms,
    }
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def build_completed_run(server: ServerHarness, run_id: str = "r1") -> dict:
    """Seed catalog (suite, cases, fixture, pool), run one run to completion
    with mixed outcomes, details and coverage fragments."""
    seed_cases(server)
    status, _ = server.json(
        "POST",
        "/v1/fixtures",
        {
            "id": "f1",
            "name": "Fix One",
            "setup_steps": [{"action": "boot", "target": "db"}],
            "teardown_steps": [{"action": "halt"}],
        },
    )
    assert status == 201
    status, _ = server.json(
        "POST", "/v1/resource-pools", {"id": "p1", "name": "Pool", "capacity": 4}
    )
    assert status == 201
    status, _ = server.json(
        "POST",
        "/v1/cases",
        case_payload(
            "c3",
            kind="api",
            fixture_ids=["f1"],
            resource_requirements={"p1": 2},
            timeout_seconds=42,
            tags=["smoke"],
        ),
    )
    assert status == 201

    status, report = server.json(
        "POST", "/v1/runs", {"id": run_id, "case_ids": ["c2", "c3"]}
    )
    assert status == 201
    assert [i["instance_id"] for i in report["instances"]] == [
        "c2[0]",
        "c2[1]",
        "c3[0]",
    ]

    assert submit(
        server,
        run_id,
        "c2[0]",
        "passed",
        12,
        details={"log": ["a", 1, None]},
        coverage={
            "files": {
                "b.py": {"executable_lines": [3, 1, 5], "covered_lines": [3, 1]},
                "a.py": {"executable_lines": [10, 20], "covered_lines": []},
            }
        },
    )[0] == 200
    assert submit(
        server,
        run_id,
        "c2[1]",
        "failed",
        7,
        details={"assert": "x"},
        coverage={
            "files": {
                "b.py": {"executable_lines": [5, 7], "covered_lines": [5, 7]},
                "中.py": {"executable_lines": [2], "covered_lines": []},
            }
        },
    )[0] == 200
    assert submit(
        server,
        run_id,
        "c3[0]",
        "error",
        9,
        details={"code": "boom"},
        coverage={
            "files": {"a.py": {"executable_lines": [10, 30], "covered_lines": [10]}}
        },
    )[0] == 200
    status, report = server.json("POST", f"/v1/runs/{run_id}/complete")
    assert status == 200
    return report


def export_raw(server: ServerHarness, run_id: str) -> tuple[int, str, bytes]:
    return server.request("GET", f"/v1/runs/{run_id}/archive")


def import_raw(server: ServerHarness, raw: bytes) -> tuple[int, str, bytes]:
    return server.request("POST", "/v1/run-archives", raw_body=raw)


class RunArchiveExportTest(unittest.TestCase):
    def test_archive_shape_is_self_contained(self) -> None:
        with ServerHarness() as server:
            original_report = build_completed_run(server)
            status, content_type, raw = export_raw(server, "r1")
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json; charset=utf-8")
            archive = json.loads(raw)

            self.assertEqual(set(archive), {"archive_version", "run", "contexts", "coverage"})
            self.assertEqual(archive["archive_version"], 1)

            # The run block is the existing full read-entry report.
            self.assertEqual(archive["run"], original_report)
            self.assertEqual(archive["run"]["status"], "completed")

            # One context per frozen instance, in frozen order.
            self.assertEqual(len(archive["contexts"]), 3)
            first = archive["contexts"][0]
            self.assertEqual(first["case_name"], "name-c2")
            self.assertEqual(first["kind"], "unit")
            self.assertEqual(first["parameters"], {"x": 1})
            self.assertEqual(first["timeout_seconds"], 300)
            self.assertEqual(first["setup"], [])
            self.assertEqual(first["teardown"], [])
            self.assertEqual(first["steps"], [{"action": "run"}])
            self.assertNotIn("resource_requirements", first)

            third = archive["contexts"][2]
            self.assertEqual(third["case_name"], "name-c3")
            self.assertEqual(third["kind"], "api")
            self.assertEqual(third["parameters"], {})
            self.assertEqual(third["timeout_seconds"], 42)
            self.assertEqual(
                third["setup"],
                [{"fixture_id": "f1", "steps": [{"action": "boot", "target": "db"}]}],
            )
            self.assertEqual(
                third["teardown"],
                [{"fixture_id": "f1", "steps": [{"action": "halt"}]}],
            )
            self.assertEqual(third["resource_requirements"], {"p1": 2})

            # Coverage is the normalized merge: sorted paths/lines, union.
            self.assertEqual(set(archive["coverage"]), {"files"})
            paths = [entry["path"] for entry in archive["coverage"]["files"]]
            self.assertEqual(paths, ["a.py", "b.py", "中.py"])
            by_path = {entry["path"]: entry for entry in archive["coverage"]["files"]}
            self.assertEqual(
                by_path["a.py"],
                {"path": "a.py", "executable_lines": [10, 20, 30], "covered_lines": [10]},
            )
            self.assertEqual(
                by_path["b.py"],
                {"path": "b.py", "executable_lines": [1, 3, 5, 7], "covered_lines": [1, 3, 5, 7]},
            )

            # No catalog objects, pool definitions, leases or other runs leak.
            for instance in archive["run"]["instances"]:
                self.assertNotIn("lease", instance)
            for context in archive["contexts"]:
                self.assertNotIn("fixture", context)
                self.assertNotIn("capacity", context)

    def test_repeated_exports_are_byte_identical(self) -> None:
        with ServerHarness() as server:
            build_completed_run(server)
            _, _, raw_a = export_raw(server, "r1")
            _, _, raw_b = export_raw(server, "r1")
            self.assertEqual(raw_b, raw_a)
            # UTF-8 body round-trips.
            self.assertEqual(raw_a.decode("utf-8").encode("utf-8"), raw_a)

    def test_export_missing_and_open_run(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            status, body = server.json("GET", "/v1/runs/ghost/archive")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, body = server.json("GET", "/v1/runs/r1/archive")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # Completing it makes the archive available; export is read-only.
            submit(server, "r1", "c1[0]")
            server.json("POST", "/v1/runs/r1/complete")
            status, _, _ = export_raw(server, "r1")
            self.assertEqual(status, 200)
            status, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(report["status"], "completed")


class RunArchiveImportTest(unittest.TestCase):
    def _roundtrip_sources(self) -> tuple[ServerHarness, bytes, dict]:
        source = ServerHarness()
        source.__enter__()
        self.addCleanup(source.__exit__, None, None, None)
        build_completed_run(source)
        _, _, raw = export_raw(source, "r1")
        return source, raw, json.loads(raw)

    def test_import_restores_full_report_without_catalog(self) -> None:
        source, raw, archive = self._roundtrip_sources()
        status, original = source.json("GET", "/v1/runs/r1")
        self.assertEqual(status, 200)

        with ServerHarness() as target:
            # Fresh process: no catalog, no pools, no runs.
            status, body = target.json("POST", "/v1/run-archives", raw_body=raw)
            self.assertEqual(status, 201, body)
            self.assertEqual(body, archive["run"])
            self.assertEqual(body, original)

            status, fetched = target.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(fetched, original)

    def test_all_read_entries_match_source_after_import(self) -> None:
        source, raw, _ = self._roundtrip_sources()
        _, _, junit_before = source.request("GET", "/v1/runs/r1/junit.xml")
        _, coverage_before = source.json("GET", "/v1/runs/r1/coverage")
        _, diagnostics_before = source.json("GET", "/v1/runs/r1/diagnostics")
        status, aggregate_before = source.json(
            "POST", "/v1/reports/aggregate", {"run_ids": ["r1"]}
        )
        self.assertEqual(status, 200)

        with ServerHarness() as target:
            status, _, _ = import_raw(target, raw)
            self.assertEqual(status, 201)

            _, _, junit_after = target.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(junit_after, junit_before)

            status, coverage_after = target.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(coverage_after, coverage_before)

            status, diagnostics_after = target.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(diagnostics_after, diagnostics_before)

            status, aggregate_after = target.json(
                "POST", "/v1/reports/aggregate", {"run_ids": ["r1"]}
            )
            self.assertEqual(status, 200)
            self.assertEqual(aggregate_after, aggregate_before)

    def test_reexport_is_byte_identical_to_source(self) -> None:
        source, raw, _ = self._roundtrip_sources()

        with ServerHarness() as target:
            status, _, _ = import_raw(target, raw)
            self.assertEqual(status, 201)
            _, _, raw_again = export_raw(target, "r1")
            self.assertEqual(raw_again, raw)

            # Imported into a second hop as well: archives are transferable
            # between any number of processes.
            with ServerHarness() as third:
                status, _, body = import_raw(third, raw_again)
                self.assertEqual(status, 201, body)
                _, _, raw_third = export_raw(third, "r1")
                self.assertEqual(raw_third, raw)

    def test_imported_completed_run_uses_run_completed_semantics(self) -> None:
        _, raw, _ = self._roundtrip_sources()
        with ServerHarness() as target:
            status, _, _ = import_raw(target, raw)
            self.assertEqual(status, 201)

            status, body = target.json(
                "POST",
                "/v1/runs/r1/claims",
                {"worker_id": "w", "lease_seconds": 10},
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_completed")

            status, body = target.json("POST", "/v1/runs/r1/timeouts", {})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_completed")

            status, body = target.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_completed")

            status, body = submit(target, "r1", "c2[0]")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_completed")

    def test_retry_after_import_preserves_frozen_lineage_and_context(self) -> None:
        _, raw, _ = self._roundtrip_sources()
        with ServerHarness() as target:
            status, _, _ = import_raw(target, raw)
            self.assertEqual(status, 201)

            # No ancestor runs were imported, but retry of the restored run
            # works purely from frozen data.
            status, retry_report = target.json(
                "POST", "/v1/runs/r1/retry", {"id": "r2"}
            )
            self.assertEqual(status, 201)
            self.assertEqual(retry_report["retry_of"], "r1")
            self.assertEqual(retry_report["root_run_id"], "r1")
            self.assertEqual(retry_report["attempt"], 1)
            self.assertEqual(
                [i["instance_id"] for i in retry_report["instances"]],
                ["c2[1]", "c3[0]"],
            )
            for instance in retry_report["instances"]:
                self.assertEqual(instance["outcome"], "pending")
                self.assertNotIn("duration_ms", instance)

            # Frozen context (fixture orchestration, timeout) survived.
            status, claims = target.json(
                "POST",
                "/v1/runs/r2/claims",
                {"worker_id": "w", "max_items": 2, "lease_seconds": 60},
            )
            self.assertEqual(status, 200)
            # c3[0] needs pool p1 which does not exist in this process, so it
            # is skipped as unsatisfiable; the frozen requirement is what
            # drove the skip (recreate the pool and it becomes claimable).
            self.assertEqual([c["instance_id"] for c in claims["claims"]], ["c2[1]"])

            status, _ = target.json(
                "POST",
                "/v1/resource-pools",
                {"id": "p1", "name": "New Pool", "capacity": 2},
            )
            self.assertEqual(status, 201)
            status, claims = target.json(
                "POST",
                "/v1/runs/r2/claims",
                {"worker_id": "w2", "max_items": 2, "lease_seconds": 60},
            )
            self.assertEqual(status, 200)
            c3_claim = claims["claims"][0]
            self.assertEqual(c3_claim["instance_id"], "c3[0]")
            self.assertEqual(c3_claim["timeout_seconds"], 42)
            self.assertEqual(
                c3_claim["setup"],
                [{"fixture_id": "f1", "steps": [{"action": "boot", "target": "db"}]}],
            )
            self.assertEqual(c3_claim["resource_requirements"], {"p1": 2})

    def test_import_retry_archive_without_ancestors(self) -> None:
        source = ServerHarness()
        source.__enter__()
        self.addCleanup(source.__exit__, None, None, None)
        build_completed_run(source)
        _, _ = source.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
        submit(source, "r2", "c2[1]", "failed", 3)
        submit(source, "r2", "c3[0]", "passed", 4)
        status, _ = source.json("POST", "/v1/runs/r2/complete")
        self.assertEqual(status, 200)
        _, _, retry_raw = export_raw(source, "r2")
        retry_archive = json.loads(retry_raw)
        self.assertEqual(retry_archive["run"]["retry_of"], "r1")
        self.assertEqual(retry_archive["run"]["root_run_id"], "r1")
        self.assertEqual(retry_archive["run"]["attempt"], 1)

        with ServerHarness() as target:
            # Neither r1 nor any catalog object exists in the target process.
            status, body = target.json("POST", "/v1/run-archives", raw_body=retry_raw)
            self.assertEqual(status, 201, body)
            self.assertEqual(body["retry_of"], "r1")
            self.assertEqual(body["root_run_id"], "r1")
            self.assertEqual(body["attempt"], 1)

            status, _, junit = target.request("GET", "/v1/runs/r2/junit.xml")
            self.assertEqual(status, 200)
            self.assertIn('name="retry_of"', junit.decode("utf-8"))

            # A chained retry keeps the frozen root and advances the attempt.
            status, chained = target.json(
                "POST", "/v1/runs/r2/retry", {"id": "r3"}
            )
            self.assertEqual(status, 201)
            self.assertEqual(chained["retry_of"], "r2")
            self.assertEqual(chained["root_run_id"], "r1")
            self.assertEqual(chained["attempt"], 2)

            # Aggregation accepts the orphaned retry and reports lineage.
            status, aggregate = target.json(
                "POST", "/v1/reports/aggregate", {"run_ids": ["r2"]}
            )
            self.assertEqual(status, 200)
            entry = aggregate["runs"][0]
            self.assertEqual(entry["run_id"], "r2")
            self.assertEqual(entry["retry_of"], "r1")
            self.assertEqual(entry["root_run_id"], "r1")
            self.assertEqual(entry["attempt"], 1)

            # Re-export is byte-identical even with no ancestors present.
            _, _, raw_again = export_raw(target, "r2")
            self.assertEqual(raw_again, retry_raw)

    def test_invalid_json(self) -> None:
        with ServerHarness() as target:
            status, _, data = target.request(
                "POST", "/v1/run-archives", raw_body=b"{not json"
            )
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(data)["error"]["code"], "invalid_json")

    def test_structural_validation_failures(self) -> None:
        _, _, good = self._roundtrip_sources()

        def mutate(**changes: object) -> dict:
            archive = copy.deepcopy(good)
            archive.update(changes)
            return archive

        # The untouched deep copy imports fine (isolation check).
        with ServerHarness() as target:
            status, body = target.json("POST", "/v1/run-archives", mutate())
            self.assertEqual(status, 201, body)

        bad_archives = [
            [],
            "archive",
            42,
            None,
            {"run": good["run"], "contexts": good["contexts"], "coverage": good["coverage"]},
            {"archive_version": 1, "contexts": good["contexts"], "coverage": good["coverage"]},
            {"archive_version": 1, "run": good["run"], "coverage": good["coverage"]},
            {"archive_version": 1, "run": good["run"], "contexts": good["contexts"]},
            {**good, "extra": 1},
            mutate(archive_version=2),
            mutate(archive_version=0),
            mutate(archive_version="1"),
            mutate(archive_version=True),
            mutate(archive_version=None),
            mutate(archive_version=1.0),
        ]
        with ServerHarness() as target:
            for archive in bad_archives:
                status, body = target.json("POST", "/v1/run-archives", archive)
                self.assertEqual(status, 400, archive)
                self.assertEqual(
                    body["error"]["code"], "validation_error", archive
                )

    def test_run_block_validation_failures(self) -> None:
        _, _, good = self._roundtrip_sources()

        def mutate_run(fn: object) -> dict:
            archive = copy.deepcopy(good)
            fn(archive["run"])
            return archive

        bad_archives = [
            mutate_run(lambda run: run.update(status="open")),
            mutate_run(lambda run: run.update(status="completed ")),
            mutate_run(lambda run: run.update(passed=True)),
            mutate_run(lambda run: run.update(passed="true")),
            mutate_run(lambda run: run.update(summary={**run["summary"], "passed": 9})),
            mutate_run(lambda run: run.update(summary={**run["summary"], "duration_ms": 99})),
            mutate_run(lambda run: run.update(summary={**run["summary"], "total": 4})),
            mutate_run(lambda run: run.pop("status")),
            mutate_run(lambda run: run.pop("passed")),
            mutate_run(lambda run: run.pop("summary")),
            mutate_run(lambda run: run.update(extra=1)),
            mutate_run(lambda run: run.update(id="")),
            mutate_run(lambda run: run.update(id="  ")),
            mutate_run(lambda run: run.update(id=123)),
            mutate_run(lambda run: run.update(instances=[])),
            mutate_run(lambda run: run.update(instances="nope")),
            mutate_run(lambda run: run.update(instances=[run["instances"][0]])),
        ]
        # Summary/passed mutation list depends on the actual outcomes; make
        # the expectations explicit: the good run has a failure and an error.
        self.assertFalse(good["run"]["passed"])

        def mutate_instance(fn: object) -> dict:
            archive = copy.deepcopy(good)
            fn(archive["run"]["instances"][0])
            return archive

        instance_mutations = [
            mutate_instance(lambda item: item.update(outcome="pending")),
            mutate_instance(lambda item: item.update(outcome="bogus")),
            mutate_instance(lambda item: item.pop("outcome")),
            mutate_instance(lambda item: item.update(duration_ms=-1)),
            mutate_instance(lambda item: item.update(duration_ms=1.5)),
            mutate_instance(lambda item: item.update(duration_ms=True)),
            mutate_instance(lambda item: item.pop("duration_ms")),
            mutate_instance(lambda item: item.update(instance_id="")),
            mutate_instance(lambda item: item.pop("instance_id")),
            mutate_instance(lambda item: item.update(case_id="")),
            mutate_instance(lambda item: item.update(case_name="  ")),
            mutate_instance(lambda item: item.update(parameters=[])),
            mutate_instance(lambda item: item.update(parameters={"bad-name": 1})),
            mutate_instance(lambda item: item.update(parameters={"a": [1]})),
            mutate_instance(lambda item: item.update(extra=1)),
        ]
        # Duplicate instance id.
        dup = copy.deepcopy(good)
        dup["run"]["instances"][1]["instance_id"] = dup["run"]["instances"][0]["instance_id"]
        instance_mutations.append(dup)

        # Mutating an instance makes its stored summary inconsistent; fix up
        # the summary for cases that test a different rule than recounting.
        with ServerHarness() as target:
            for archive in bad_archives + instance_mutations:
                status, body = target.json("POST", "/v1/run-archives", archive)
                self.assertEqual(status, 400, json.dumps(archive)[:400])
                self.assertEqual(
                    body["error"]["code"], "validation_error",
                    json.dumps(archive)[:400],
                )

    def test_context_validation_failures(self) -> None:
        _, _, good = self._roundtrip_sources()

        def mutate_context(index: int, fn: object) -> dict:
            archive = copy.deepcopy(good)
            fn(archive["contexts"][index])
            return archive

        bad_archives = [
            {**good, "contexts": good["contexts"][:-1]},
            {**good, "contexts": good["contexts"] + [good["contexts"][0]]},
            {**good, "contexts": "nope"},
            mutate_context(0, lambda c: c.update(kind="browser ")),
            mutate_context(0, lambda c: c.update(kind="nope")),
            mutate_context(0, lambda c: c.pop("kind")),
            mutate_context(0, lambda c: c.update(timeout_seconds=0)),
            mutate_context(0, lambda c: c.update(timeout_seconds="300")),
            mutate_context(0, lambda c: c.update(timeout_seconds=True)),
            mutate_context(2, lambda c: c.update(setup={})),
            mutate_context(2, lambda c: c.update(setup=[{"steps": []}])),
            mutate_context(2, lambda c: c.update(setup=[{"fixture_id": "f1"}])),
            mutate_context(2, lambda c: c.update(setup=[{"fixture_id": "", "steps": []}])),
            mutate_context(2, lambda c: c.update(setup=[{"fixture_id": "f1", "steps": [{}]}])),
            mutate_context(2, lambda c: c.update(steps=[])),
            mutate_context(2, lambda c: c.update(steps=[{}])),
            mutate_context(2, lambda c: c.pop("steps")),
            mutate_context(2, lambda c: c.update(case_name="other")),
            mutate_context(0, lambda c: c.update(parameters={"x": 2})),
            mutate_context(0, lambda c: c.pop("parameters")),
            mutate_context(2, lambda c: c.update(resource_requirements={"p1": 0})),
            mutate_context(2, lambda c: c.update(resource_requirements={"p1": "2"})),
            mutate_context(0, lambda c: c.update(extra=1)),
        ]
        with ServerHarness() as target:
            for archive in bad_archives:
                status, body = target.json("POST", "/v1/run-archives", archive)
                self.assertEqual(status, 400, json.dumps(archive)[:400])
                self.assertEqual(
                    body["error"]["code"], "validation_error",
                    json.dumps(archive)[:400],
                )

    def test_coverage_validation_failures(self) -> None:
        _, _, good = self._roundtrip_sources()

        def mutate_coverage(fn: object) -> dict:
            archive = copy.deepcopy(good)
            fn(archive["coverage"])
            return archive

        def mutate_file(index: int, fn: object) -> dict:
            archive = copy.deepcopy(good)
            fn(archive["coverage"]["files"][index])
            return archive

        bad_archives = [
            {**good, "coverage": None},
            {**good, "coverage": []},
            mutate_coverage(lambda c: c.update(extra=1)),
            mutate_coverage(lambda c: c.pop("files")),
            mutate_coverage(lambda c: c.update(files={})),
            mutate_coverage(lambda c: c.update(files="nope")),
            mutate_coverage(lambda c: c.update(files=[None])),
            mutate_file(0, lambda f: f.update(extra=1)),
            mutate_file(0, lambda f: f.pop("path")),
            mutate_file(0, lambda f: f.update(path="")),
            mutate_file(0, lambda f: f.pop("executable_lines")),
            mutate_file(0, lambda f: f.pop("covered_lines")),
            mutate_file(0, lambda f: f.update(executable_lines=[])),
            mutate_file(0, lambda f: f.update(executable_lines=[1, 1])),
            mutate_file(0, lambda f: f.update(executable_lines=[3, 2])),
            mutate_file(0, lambda f: f.update(executable_lines=[0])),
            mutate_file(0, lambda f: f.update(executable_lines=[True])),
            mutate_file(0, lambda f: f.update(covered_lines=[2])),
            mutate_file(0, lambda f: f.update(covered_lines=[10, 10])),
        ]
        # Duplicate coverage path.
        duplicate_paths = copy.deepcopy(good)
        duplicate_paths["coverage"]["files"][1]["path"] = (
            duplicate_paths["coverage"]["files"][0]["path"]
        )
        bad_archives.append(duplicate_paths)

        # Files must already be in normalized Unicode code-point order.
        unsorted_paths = copy.deepcopy(good)
        unsorted_paths["coverage"]["files"][0], unsorted_paths["coverage"]["files"][1] = (
            unsorted_paths["coverage"]["files"][1],
            unsorted_paths["coverage"]["files"][0],
        )
        bad_archives.append(unsorted_paths)

        with ServerHarness() as target:
            for archive in bad_archives:
                status, body = target.json("POST", "/v1/run-archives", archive)
                self.assertEqual(status, 400, json.dumps(archive)[:400])
                self.assertEqual(
                    body["error"]["code"], "validation_error",
                    json.dumps(archive)[:400],
                )

            # Empty-coverage archives are complete archives and import fine.
            no_cov = copy.deepcopy(good)
            no_cov["coverage"] = {"files": []}
            status, body = target.json("POST", "/v1/run-archives", no_cov)
            self.assertEqual(status, 201, body)
            status, coverage = target.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(coverage["files"], [])
            self.assertIsNone(coverage["summary"]["coverage_percent"])

    def test_lineage_validation_failures(self) -> None:
        source = ServerHarness()
        source.__enter__()
        self.addCleanup(source.__exit__, None, None, None)
        build_completed_run(source)
        source.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
        submit(source, "r2", "c2[1]", "failed", 3)
        submit(source, "r2", "c3[0]", "passed", 4)
        source.json("POST", "/v1/runs/r2/complete")
        _, _, raw = export_raw(source, "r2")
        good_retry = json.loads(raw)

        def mutate_run(archive: dict, fn: object) -> dict:
            result = copy.deepcopy(archive)
            fn(result["run"])
            return result

        bad_archives = [
            mutate_run(good_retry, lambda r: r.pop("retry_of")),
            mutate_run(good_retry, lambda r: r.pop("root_run_id")),
            mutate_run(good_retry, lambda r: r.pop("attempt")),
            mutate_run(good_retry, lambda r: r.update(attempt=0)),
            mutate_run(good_retry, lambda r: r.update(attempt="1")),
            mutate_run(good_retry, lambda r: r.update(attempt=True)),
            mutate_run(good_retry, lambda r: r.update(retry_of="")),
            mutate_run(good_retry, lambda r: r.update(root_run_id=7)),
            mutate_run(good_retry, lambda r: r.update(retry_of="r2")),
            mutate_run(good_retry, lambda r: r.update(root_run_id="other")),
        ]
        # A normal run with only some lineage fields is inconsistent; all
        # three together would instead describe a valid retry run.
        _, _, normal_raw = export_raw(source, "r1")
        normal = json.loads(normal_raw)
        bad_archives.append(
            mutate_run(normal, lambda r: r.update(retry_of="x"))
        )
        bad_archives.append(
            mutate_run(normal, lambda r: r.update(retry_of="x", root_run_id="y"))
        )

        with ServerHarness() as target:
            for archive in bad_archives:
                status, body = target.json("POST", "/v1/run-archives", archive)
                self.assertEqual(status, 400, json.dumps(archive)[:400])
                self.assertEqual(
                    body["error"]["code"], "validation_error",
                    json.dumps(archive)[:400],
                )

    def test_run_exists_conflict(self) -> None:
        _, raw, _ = self._roundtrip_sources()
        with ServerHarness() as target:
            status, _, _ = import_raw(target, raw)
            self.assertEqual(status, 201)
            status, _, data = import_raw(target, raw)
            self.assertEqual(status, 409)
            self.assertEqual(json.loads(data)["error"]["code"], "run_exists")

        # Also conflicts with a run created the normal way.
        with ServerHarness() as target:
            seed_cases(target)
            target.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, _, data = import_raw(target, raw)
            self.assertEqual(status, 409)
            self.assertEqual(json.loads(data)["error"]["code"], "run_exists")

    def test_failed_import_leaves_no_state(self) -> None:
        _, _, good = self._roundtrip_sources()
        broken = copy.deepcopy(good)
        broken["run"]["summary"]["failed"] = 99

        with ServerHarness() as target:
            status, body = target.json("POST", "/v1/run-archives", broken)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            # The id was not occupied by the failed import.
            status, body = target.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")
            status, body = target.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")
            status, body = target.json(
                "POST", "/v1/reports/aggregate", {"run_ids": ["r1"]}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            # A subsequent valid import with the same id succeeds.
            status, _, data = target.request(
                "POST",
                "/v1/run-archives",
                raw_body=json.dumps(good).encode("utf-8"),
            )
            self.assertEqual(status, 201, data)
            status, fetched = target.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(fetched, good["run"])

    def test_import_is_isolated_from_request_mutation(self) -> None:
        _, _, good = self._roundtrip_sources()
        with ServerHarness() as target:
            payload = copy.deepcopy(good)
            status, _ = target.json("POST", "/v1/run-archives", payload)
            self.assertEqual(status, 201)
            # Mutating the caller's object after import cannot change state.
            payload["run"]["instances"][0]["outcome"] = "failed"
            payload["contexts"][0]["steps"].append({"action": "tamper"})
            status, fetched = target.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(fetched, good["run"])
            _, _, raw_again = export_raw(target, "r1")
            self.assertEqual(json.loads(raw_again), good)


if __name__ == "__main__":
    unittest.main()
