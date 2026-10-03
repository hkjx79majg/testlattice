"""End-to-end HTTP tests for run failure diagnostics export."""

from __future__ import annotations

import unittest

from test_runs import ServerHarness, case_payload


def seed_with_fixtures(server: ServerHarness) -> None:
    server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
    server.json(
        "POST",
        "/v1/fixtures",
        {
            "id": "f1",
            "name": "F1",
            "setup_steps": [{"action": "f1-setup"}],
            "teardown_steps": [{"action": "f1-teardown"}],
        },
    )
    server.json(
        "POST",
        "/v1/fixtures",
        {
            "id": "f2",
            "name": "F2",
            "setup_steps": [{"action": "f2-setup"}],
            "teardown_steps": [{"action": "f2-teardown"}],
            "dependencies": ["f1"],
        },
    )
    server.json(
        "POST",
        "/v1/cases",
        case_payload(
            "c1",
            kind="api",
            timeout_seconds=30,
            steps=[{"action": "c1-step", "extra": {"nested": [1, 2]}}],
            fixture_ids=["f2"],
            parameterization={"axes": {"x": [1, 2]}},
        ),
    )
    server.json(
        "POST",
        "/v1/cases",
        case_payload("c2", kind="browser", timeout_seconds=5),
    )


def finish_with(server: ServerHarness, run_id: str, results: dict) -> None:
    for instance_id, spec in results.items():
        outcome, extra = spec if isinstance(spec, tuple) else (spec, {})
        payload = {"instance_id": instance_id, "outcome": outcome, "duration_ms": 5}
        payload.update(extra)
        status, _ = server.json("POST", f"/v1/runs/{run_id}/results", payload)
        assert status == 200, (instance_id, status)
    status, _ = server.json("POST", f"/v1/runs/{run_id}/complete")
    assert status == 200


class DiagnosticsTest(unittest.TestCase):
    def test_failed_and_error_instances_exported_in_frozen_order(self) -> None:
        with ServerHarness() as server:
            seed_with_fixtures(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1", "c2"]})
            finish_with(
                server,
                "r1",
                {
                    "c1[0]": "passed",
                    "c1[1]": ("failed", {"details": {"log": ["a", 1, None]}}),
                    "c2[0]": "error",
                },
            )

            status, report = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(report["run_id"], "r1")
            self.assertEqual(
                report["summary"], {"total": 2, "failed": 1, "error": 1}
            )
            self.assertNotIn("retry_of", report)
            self.assertNotIn("root_run_id", report)
            self.assertNotIn("attempt", report)

            failures = report["failures"]
            self.assertEqual(
                [item["instance_id"] for item in failures], ["c1[1]", "c2[0]"]
            )

            first = failures[0]
            self.assertEqual(first["case_id"], "c1")
            self.assertEqual(first["case_name"], "name-c1")
            self.assertEqual(first["kind"], "api")
            self.assertEqual(first["parameters"], {"x": 2})
            self.assertEqual(first["outcome"], "failed")
            self.assertEqual(first["duration_ms"], 5)
            self.assertEqual(first["timeout_seconds"], 30)
            self.assertEqual(
                first["steps"], [{"action": "c1-step", "extra": {"nested": [1, 2]}}]
            )
            # Fixture closure: dependencies first in setup, reversed in teardown.
            self.assertEqual(
                first["setup"],
                [
                    {"fixture_id": "f1", "steps": [{"action": "f1-setup"}]},
                    {"fixture_id": "f2", "steps": [{"action": "f2-setup"}]},
                ],
            )
            self.assertEqual(
                first["teardown"],
                [
                    {"fixture_id": "f2", "steps": [{"action": "f2-teardown"}]},
                    {"fixture_id": "f1", "steps": [{"action": "f1-teardown"}]},
                ],
            )
            self.assertEqual(first["details"], {"log": ["a", 1, None]})

            second = failures[1]
            self.assertEqual(second["case_id"], "c2")
            self.assertEqual(second["kind"], "browser")
            self.assertEqual(second["parameters"], {})
            self.assertEqual(second["outcome"], "error")
            self.assertEqual(second["timeout_seconds"], 5)
            self.assertEqual(second["setup"], [])
            self.assertEqual(second["teardown"], [])
            self.assertEqual(second["steps"], [{"action": "run"}])
            # details only appears when the original result carried it.
            self.assertNotIn("details", second)

    def test_frozen_context_survives_catalog_changes(self) -> None:
        with ServerHarness() as server:
            seed_with_fixtures(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "failed", "c1[1]": "error"})

            status, before = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)

            # Deleting the case and then the fixtures must not change the export.
            server.request("DELETE", "/v1/cases/c1")
            server.request("DELETE", "/v1/fixtures/f2")
            server.request("DELETE", "/v1/fixtures/f1")

            status, after = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(after, before)
            self.assertEqual(after["failures"][0]["kind"], "api")
            self.assertEqual(len(after["failures"][0]["setup"]), 2)

            # Repeated reads of an unchanged run are identical.
            status, again = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(again, before)

    def test_response_is_isolated_from_internal_state(self) -> None:
        with ServerHarness() as server:
            seed_with_fixtures(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(
                server,
                "r1",
                {"c1[0]": "passed", "c1[1]": ("failed", {"details": {"log": []}})},
            )
            status, report = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)

            # Mutating the returned payload must not leak into later reads.
            report["failures"][0]["steps"].append({"action": "hacked"})
            report["failures"][0]["setup"][0]["steps"].clear()
            report["failures"][0]["details"]["log"].append("hacked")
            report["failures"][0]["parameters"]["x"] = 99

            status, fresh = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(
                fresh["failures"][0]["steps"],
                [{"action": "c1-step", "extra": {"nested": [1, 2]}}],
            )
            self.assertEqual(
                fresh["failures"][0]["setup"][0]["steps"], [{"action": "f1-setup"}]
            )
            self.assertEqual(fresh["failures"][0]["details"], {"log": []})
            self.assertEqual(fresh["failures"][0]["parameters"], {"x": 2})

    def test_no_failures_returns_zero_counts_and_empty_failures(self) -> None:
        with ServerHarness() as server:
            seed_with_fixtures(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1", "c2"]})
            finish_with(
                server,
                "r1",
                {"c1[0]": "passed", "c1[1]": "skipped", "c2[0]": "passed"},
            )
            status, report = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(report["run_id"], "r1")
            self.assertEqual(
                report["summary"], {"total": 0, "failed": 0, "error": 0}
            )
            self.assertEqual(report["failures"], [])

    def test_missing_and_incomplete_runs(self) -> None:
        with ServerHarness() as server:
            seed_with_fixtures(server)
            status, body = server.json("GET", "/v1/runs/ghost/diagnostics")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            status, body = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # Unknown paths keep the generic 404.
            status, body = server.json("GET", "/v1/runs/r1/diagnostics/extra")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")

    def test_retry_run_diagnostics_carry_retry_fields_and_frozen_context(self) -> None:
        with ServerHarness() as server:
            seed_with_fixtures(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish_with(server, "r1", {"c1[0]": "passed", "c1[1]": "failed"})

            # Wipe the catalog before retrying; the retry copies the frozen
            # context from its direct source run.
            server.request("DELETE", "/v1/cases/c1")
            server.request("DELETE", "/v1/cases/c2")
            server.request("DELETE", "/v1/fixtures/f2")
            server.request("DELETE", "/v1/fixtures/f1")

            status, retry = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            finish_with(server, "r2", {"c1[1]": "error"})

            status, report = server.json("GET", "/v1/runs/r2/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(report["run_id"], "r2")
            self.assertEqual(report["retry_of"], "r1")
            self.assertEqual(report["root_run_id"], "r1")
            self.assertEqual(report["attempt"], 1)
            self.assertEqual(
                report["summary"], {"total": 1, "failed": 0, "error": 1}
            )
            self.assertEqual(len(report["failures"]), 1)
            entry = report["failures"][0]
            self.assertEqual(entry["instance_id"], "c1[1]")
            self.assertEqual(entry["kind"], "api")
            self.assertEqual(entry["timeout_seconds"], 30)
            self.assertEqual(entry["parameters"], {"x": 2})
            self.assertEqual(len(entry["setup"]), 2)
            self.assertEqual(len(entry["teardown"]), 2)

            # A chained retry copies the context from its direct source too.
            status, chained = server.json("POST", "/v1/runs/r2/retry", {"id": "r3"})
            self.assertEqual(status, 201)
            finish_with(server, "r3", {"c1[1]": "failed"})
            status, report = server.json("GET", "/v1/runs/r3/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(report["retry_of"], "r2")
            self.assertEqual(report["root_run_id"], "r1")
            self.assertEqual(report["attempt"], 2)
            entry = report["failures"][0]
            self.assertEqual(entry["kind"], "api")
            self.assertEqual(entry["timeout_seconds"], 30)
            self.assertEqual(len(entry["setup"]), 2)

    def test_run_report_and_other_exports_unchanged(self) -> None:
        with ServerHarness() as server:
            seed_with_fixtures(server)
            status, report = server.json(
                "POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]}
            )
            self.assertEqual(status, 201)
            # The frozen diagnostics context must not leak into the run report.
            for instance in report["instances"]:
                self.assertNotIn("kind", instance)
                self.assertNotIn("timeout_seconds", instance)
                self.assertNotIn("steps", instance)
                self.assertNotIn("setup", instance)
                self.assertNotIn("teardown", instance)
            finish_with(server, "r1", {"c1[0]": "failed", "c1[1]": "passed"})
            status, fetched = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            for instance in fetched["instances"]:
                self.assertNotIn("kind", instance)
                self.assertNotIn("steps", instance)
            status, _, _ = server.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(status, 200)
            status, _ = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            status, _ = server.json("POST", "/v1/reports/aggregate", {"run_ids": ["r1"]})
            self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
