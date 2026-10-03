"""End-to-end HTTP tests for completed-run failure diagnostics export."""

from __future__ import annotations

import json
import unittest

from test_runs import ServerHarness, case_payload, seed_cases


def submit(server: ServerHarness, run_id: str, instance_id: str, outcome: str, **extra):
    payload = {"instance_id": instance_id, "outcome": outcome, "duration_ms": 5}
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def complete(server: ServerHarness, run_id: str):
    return server.json("POST", f"/v1/runs/{run_id}/complete")


def fixture_payload(fixture_id, **overrides):
    payload = {
        "id": fixture_id,
        "name": f"name-{fixture_id}",
        "setup_steps": [{"action": f"{fixture_id}-setup"}],
        "teardown_steps": [{"action": f"{fixture_id}-teardown"}],
    }
    payload.update(overrides)
    return payload


def seed_fixture_case(server):
    server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
    server.json(
        "POST",
        "/v1/fixtures",
        fixture_payload("db", setup_steps=[{"action": "db-up"}], teardown_steps=[{"action": "db-down"}]),
    )
    server.json(
        "POST",
        "/v1/fixtures",
        fixture_payload("app", dependencies=["db"]),
    )
    server.json(
        "POST",
        "/v1/cases",
        case_payload(
            "cf",
            kind="api",
            timeout_seconds=42,
            steps=[{"action": "step1"}, {"action": "step2"}],
            fixture_ids=["app"],
            parameterization={"axes": {"x": [1, 2]}},
        ),
    )


class DiagnosticsTest(unittest.TestCase):
    def test_exports_only_failed_and_error_in_frozen_order(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            submit(server, "r1", "c2[0]", "failed", duration_ms=11, details={"a": 1})
            submit(server, "r1", "c2[1]", "passed", duration_ms=3)
            submit(server, "r1", "c1[0]", "error", duration_ms=7, details=["boom"])
            # skipped is neither a failure nor an error; force a 4th instance
            # by adding a skipped-only case.
            server.json(
                "POST",
                "/v1/cases",
                case_payload("c3", parameterization={"axes": {"y": ["p"]}}),
            )
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c3"]})
            submit(server, "r2", "c3[0]", "skipped", duration_ms=0)
            complete(server, "r1")
            complete(server, "r2")

            status, body = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(body["run_id"], "r1")
            self.assertEqual(body["summary"], {"total": 2, "failed": 1, "error": 1})
            self.assertNotIn("retry_of", body)
            self.assertNotIn("root_run_id", body)
            self.assertNotIn("attempt", body)
            self.assertEqual(
                [f["instance_id"] for f in body["failures"]],
                ["c2[0]", "c1[0]"],
            )

            failed = body["failures"][0]
            self.assertEqual(failed["case_id"], "c2")
            self.assertEqual(failed["case_name"], "name-c2")
            self.assertEqual(failed["kind"], "unit")
            self.assertEqual(failed["parameters"], {"x": 1})
            self.assertEqual(failed["outcome"], "failed")
            self.assertEqual(failed["duration_ms"], 11)
            self.assertEqual(failed["timeout_seconds"], 300)
            self.assertEqual(failed["details"], {"a": 1})
            self.assertEqual(failed["setup"], [])
            self.assertEqual(failed["steps"], [{"action": "run"}])
            self.assertEqual(failed["teardown"], [])

            errored = body["failures"][1]
            self.assertEqual(errored["outcome"], "error")
            self.assertEqual(errored["duration_ms"], 7)
            self.assertEqual(errored["details"], ["boom"])

            # A run whose only non-pending instance was skipped exports nothing.
            status, body = server.json("GET", "/v1/runs/r2/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(body["summary"], {"total": 0, "failed": 0, "error": 0})
            self.assertEqual(body["failures"], [])

    def test_details_only_present_when_originally_submitted(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
            submit(server, "r1", "c2[0]", "failed", duration_ms=1, details=None)
            submit(server, "r1", "c2[1]", "error", duration_ms=2)
            complete(server, "r1")
            _, body = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertIn("details", body["failures"][0])
            self.assertIsNone(body["failures"][0]["details"])
            self.assertNotIn("details", body["failures"][1])

    def test_frozen_context_includes_fixture_plan_and_survives_catalog_changes(self) -> None:
        with ServerHarness() as server:
            seed_fixture_case(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cf"]})
            submit(server, "r1", "cf[0]", "failed", duration_ms=4)
            submit(server, "r1", "cf[1]", "passed", duration_ms=4)
            complete(server, "r1")

            # Delete and rewrite both fixtures and the case after completion.
            server.request("DELETE", "/v1/cases/cf")
            server.request("DELETE", "/v1/fixtures/app")
            server.request("DELETE", "/v1/fixtures/db")
            server.json(
                "POST",
                "/v1/cases",
                case_payload("cf", steps=[{"action": "MUTATED"}], timeout_seconds=9, kind="browser"),
            )

            _, first = server.json("GET", "/v1/runs/r1/diagnostics")
            _, second = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(first, second)

            failure = first["failures"][0]
            self.assertEqual(failure["kind"], "api")
            self.assertEqual(failure["timeout_seconds"], 42)
            self.assertEqual(failure["steps"], [{"action": "step1"}, {"action": "step2"}])
            self.assertEqual(
                [g["fixture_id"] for g in failure["setup"]],
                ["db", "app"],
            )
            self.assertEqual(
                failure["setup"],
                [
                    {"fixture_id": "db", "steps": [{"action": "db-up"}]},
                    {"fixture_id": "app", "steps": [{"action": "app-setup"}]},
                ],
            )
            self.assertEqual(
                [g["fixture_id"] for g in failure["teardown"]],
                ["app", "db"],
            )
            self.assertEqual(
                failure["teardown"],
                [
                    {"fixture_id": "app", "steps": [{"action": "app-teardown"}]},
                    {"fixture_id": "db", "steps": [{"action": "db-down"}]},
                ],
            )

    def test_retry_diagnostics_carry_chain_fields_and_copied_context(self) -> None:
        with ServerHarness() as server:
            seed_fixture_case(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cf"]})
            submit(server, "r1", "cf[0]", "failed", duration_ms=4)
            submit(server, "r1", "cf[1]", "passed", duration_ms=4)
            complete(server, "r1")

            status, r2 = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            submit(server, "r2", "cf[0]", "error", duration_ms=8, details={"n": 2})
            complete(server, "r2")

            # Wipe the catalog before reading either export; retries must not
            # have consulted it.
            server.request("DELETE", "/v1/cases/cf")
            server.request("DELETE", "/v1/fixtures/app")
            server.request("DELETE", "/v1/fixtures/db")

            _, d2 = server.json("GET", "/v1/runs/r2/diagnostics")
            self.assertEqual(d2["run_id"], "r2")
            self.assertEqual(d2["retry_of"], "r1")
            self.assertEqual(d2["root_run_id"], "r1")
            self.assertEqual(d2["attempt"], 1)
            self.assertEqual([f["instance_id"] for f in d2["failures"]], ["cf[0]"])
            failure = d2["failures"][0]
            self.assertEqual(failure["kind"], "api")
            self.assertEqual(failure["timeout_seconds"], 42)
            self.assertEqual([g["fixture_id"] for g in failure["setup"]], ["db", "app"])
            self.assertEqual([g["fixture_id"] for g in failure["teardown"]], ["app", "db"])
            self.assertEqual(failure["details"], {"n": 2})

            # Chain another retry; root stays the original run, attempt grows.
            status, r3 = server.json("POST", "/v1/runs/r2/retry", {"id": "r3"})
            self.assertEqual(status, 201)
            submit(server, "r3", "cf[0]", "failed", duration_ms=1)
            complete(server, "r3")
            _, d3 = server.json("GET", "/v1/runs/r3/diagnostics")
            self.assertEqual(d3["retry_of"], "r2")
            self.assertEqual(d3["root_run_id"], "r1")
            self.assertEqual(d3["attempt"], 2)
            self.assertEqual(d3["failures"][0]["steps"], [{"action": "step1"}, {"action": "step2"}])
            self.assertNotIn("details", d3["failures"][0])

            # The source run's diagnostics are untouched by the chain.
            _, d1 = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertNotIn("retry_of", d1)
            self.assertEqual(d1["failures"][0]["outcome"], "failed")
            self.assertEqual(d1["failures"][0]["duration_ms"], 4)

    def test_all_passing_run_exports_empty_failures(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            submit(server, "r1", "c2[0]", "passed", duration_ms=1)
            submit(server, "r1", "c2[1]", "passed", duration_ms=1)
            submit(server, "r1", "c1[0]", "passed", duration_ms=1)
            complete(server, "r1")
            status, body = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)
            self.assertEqual(body["summary"], {"total": 0, "failed": 0, "error": 0})
            self.assertEqual(body["failures"], [])

    def test_missing_and_incomplete_run_status_codes(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            status, body = server.json("GET", "/v1/runs/ghost/diagnostics")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "run_not_found")

            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, body = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

            # Completed with a submitted result works afterwards.
            submit(server, "r1", "c1[0]", "passed", duration_ms=1)
            complete(server, "r1")
            status, _ = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(status, 200)

    def test_unknown_subpath_is_not_found(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(server, "r1", "c1[0]", "passed", duration_ms=1)
            complete(server, "r1")
            for path in ("/v1/runs/r1/diagnostics/extra", "/v1/runs//diagnostics"):
                status, body = server.json("GET", path)
                self.assertEqual(status, 404, path)
                self.assertEqual(body["error"]["code"], "not_found", path)

    def test_repeated_reads_and_response_isolation(self) -> None:
        with ServerHarness() as server:
            seed_fixture_case(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cf"]})
            submit(server, "r1", "cf[0]", "failed", duration_ms=4, details={"k": "v"})
            submit(server, "r1", "cf[1]", "error", duration_ms=5)
            complete(server, "r1")

            _, first = server.json("GET", "/v1/runs/r1/diagnostics")
            # Tamper with the decoded response, then re-read.
            first["failures"][0]["setup"].append({"fixture_id": "fake", "steps": []})
            first["failures"][0]["steps"].append({"action": "tamper"})
            first["failures"][0]["details"]["k"] = "mutated"
            _, second = server.json("GET", "/v1/runs/r1/diagnostics")
            _, third = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(second, third)
            self.assertEqual(
                [g["fixture_id"] for g in second["failures"][0]["setup"]],
                ["db", "app"],
            )
            self.assertEqual(
                second["failures"][0]["steps"],
                [{"action": "step1"}, {"action": "step2"}],
            )
            self.assertEqual(second["failures"][0]["details"], {"k": "v"})

    def test_diagnostics_does_not_modify_run_report_or_coverage(self) -> None:
        with ServerHarness() as server:
            seed_fixture_case(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cf"]})
            submit(
                server,
                "r1",
                "cf[0]",
                "failed",
                duration_ms=4,
                coverage={
                    "files": {
                        "a.py": {"executable_lines": [1, 2], "covered_lines": [1]}
                    }
                },
            )
            submit(server, "r1", "cf[1]", "passed", duration_ms=4)
            _, completed = complete(server, "r1")

            _, diag1 = server.json("GET", "/v1/runs/r1/diagnostics")
            _, diag2 = server.json("GET", "/v1/runs/r1/diagnostics")
            self.assertEqual(diag1, diag2)

            _, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(report, completed)
            self.assertNotIn("setup", report["instances"][0])

            status, coverage = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(coverage["summary"]["covered_lines"], 1)


if __name__ == "__main__":
    unittest.main()
