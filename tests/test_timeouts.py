"""End-to-end HTTP tests for server-side timeout detection on leased runs."""

from __future__ import annotations

import unittest
from unittest import mock

import testlattice.service as service_module
from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases


def claim(server: ServerHarness, run_id: str, **body: object):
    payload = {"worker_id": "w", "lease_seconds": 3600}
    payload.update(body)
    return server.json("POST", f"/v1/runs/{run_id}/claims", payload)


def timeouts(server: ServerHarness, run_id: str, body: object = ...):
    if body is ...:
        body = {}
    return server.json("POST", f"/v1/runs/{run_id}/timeouts", body)


class TimeoutTest(unittest.TestCase):
    def test_timeout_writes_error_result_with_fixed_details(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            service = Service()
            with ServerHarness(service) as server:
                seed_cases(server)
                server.json(
                    "POST",
                    "/v1/cases",
                    case_payload("slow", timeout_seconds=5),
                )
                server.json(
                    "POST", "/v1/runs", {"id": "r1", "case_ids": ["slow", "c1"]}
                )
                status, body = claim(
                    server, "r1", worker_id="worker-7", max_items=2
                )
                self.assertEqual(status, 200)
                claims = {c["instance_id"]: c for c in body["claims"]}
                self.assertEqual(set(claims), {"slow[0]", "c1[0]"})
                claimed_at = clock["now"]

                # One millisecond before the deadline: nothing times out.
                clock["now"] = claimed_at + 5_000 - 1
                status, body = timeouts(server, "r1")
                self.assertEqual(status, 200)
                self.assertEqual(body["timed_out"], [])
                self.assertEqual(body["run"]["summary"]["pending"], 2)

                # Exactly at claimed_at + timeout: only the 5s case times out;
                # c1[0] keeps the default 300s timeout.
                clock["now"] = claimed_at + 5_000
                status, body = timeouts(server, "r1")
                self.assertEqual(status, 200)
                self.assertEqual(body["timed_out"], ["slow[0]"])
                report = body["run"]
                self.assertEqual(report["status"], "open")
                self.assertEqual(report["summary"]["error"], 1)
                self.assertEqual(report["summary"]["pending"], 1)
                self.assertEqual(report["summary"]["duration_ms"], 5_000)
                instance = report["instances"][0]
                self.assertEqual(instance["instance_id"], "slow[0]")
                self.assertEqual(instance["outcome"], "error")
                self.assertEqual(instance["duration_ms"], 5_000)
                self.assertEqual(
                    instance["details"],
                    {
                        "code": "timeout",
                        "timeout_seconds": 5,
                        "worker_id": "worker-7",
                        "claimed_at": claimed_at,
                        "deadline_at": claimed_at + 5_000,
                        "detected_at": claimed_at + 5_000,
                    },
                )
                self.assertNotIn("lease", instance)
                self.assertNotIn("coverage", instance)

                # A second scan finds nothing new.
                status, body = timeouts(server, "r1")
                self.assertEqual(status, 200)
                self.assertEqual(body["timed_out"], [])

    def test_late_submit_with_old_claim_loses_after_timeout(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            service = Service()
            with ServerHarness(service) as server:
                seed_cases(server)
                server.json(
                    "POST",
                    "/v1/cases",
                    case_payload("slow", timeout_seconds=1),
                )
                server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["slow"]})
                _, body = claim(server, "r1", worker_id="w")
                claim_id = body["claims"][0]["claim_id"]

                clock["now"] += 1_000
                status, body = timeouts(server, "r1")
                self.assertEqual(status, 200)
                self.assertEqual(body["timed_out"], ["slow[0]"])

                # The late submission with the original claim id is rejected
                # and must not overwrite the timeout result.
                status, error = server.json(
                    "POST",
                    "/v1/runs/r1/results",
                    {
                        "instance_id": "slow[0]",
                        "outcome": "passed",
                        "duration_ms": 3,
                        "claim_id": claim_id,
                    },
                )
                self.assertEqual(status, 409)
                self.assertEqual(error["error"]["code"], "claim_not_active")

                status, report = server.json("GET", "/v1/runs/r1")
                self.assertEqual(status, 200)
                instance = report["instances"][0]
                self.assertEqual(instance["outcome"], "error")
                self.assertEqual(instance["duration_ms"], 1_000)
                self.assertEqual(instance["details"]["code"], "timeout")

    def test_expired_lease_is_dropped_not_timed_out(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            service = Service()
            with ServerHarness(service) as server:
                seed_cases(server)
                server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
                # Short lease, long (default 300s) timeout: the lease dies
                # by expires_at long before the timeout deadline.
                _, body = claim(server, "r1", lease_seconds=2)
                self.assertEqual(len(body["claims"]), 1)

                clock["now"] += 3_000
                status, body = timeouts(server, "r1")
                self.assertEqual(status, 200)
                self.assertEqual(body["timed_out"], [])
                self.assertEqual(body["run"]["summary"]["pending"], 1)

                # The instance is simply claimable again.
                status, body = claim(server, "r1", worker_id="w2")
                self.assertEqual(status, 200)
                self.assertEqual(
                    [c["instance_id"] for c in body["claims"]], ["c1[0]"]
                )

    def test_instances_without_live_lease_are_untouched(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            service = Service()
            with ServerHarness(service) as server:
                seed_cases(server)
                server.json(
                    "POST",
                    "/v1/cases",
                    case_payload("slow", timeout_seconds=1),
                )
                server.json(
                    "POST", "/v1/runs", {"id": "r1", "case_ids": ["slow", "c1"]}
                )
                # c1[0] gets a direct result; slow[0] is never leased.
                status, _ = server.json(
                    "POST",
                    "/v1/runs/r1/results",
                    {
                        "instance_id": "c1[0]",
                        "outcome": "passed",
                        "duration_ms": 4,
                    },
                )
                self.assertEqual(status, 200)

                clock["now"] += 600_000
                status, body = timeouts(server, "r1")
                self.assertEqual(status, 200)
                self.assertEqual(body["timed_out"], [])
                report = body["run"]
                self.assertEqual(report["summary"]["passed"], 1)
                self.assertEqual(report["summary"]["pending"], 1)
                self.assertEqual(report["summary"]["duration_ms"], 4)

    def test_timeout_result_feeds_reports_diagnostics_and_retry(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            service = Service()
            with ServerHarness(service) as server:
                seed_cases(server)
                server.json(
                    "POST",
                    "/v1/cases",
                    case_payload("slow", timeout_seconds=2),
                )
                server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["slow"]})
                claim(server, "r1", worker_id="w")
                clock["now"] += 2_000
                status, body = timeouts(server, "r1")
                self.assertEqual(body["timed_out"], ["slow[0]"])

                # The run is not auto-completed but can be completed now.
                status, report = server.json("POST", "/v1/runs/r1/complete")
                self.assertEqual(status, 200)
                self.assertEqual(report["status"], "completed")
                self.assertFalse(report["passed"])

                # Diagnostics export carries the timeout details.
                status, diagnostics = server.json("GET", "/v1/runs/r1/diagnostics")
                self.assertEqual(status, 200)
                self.assertEqual(
                    diagnostics["summary"], {"total": 1, "failed": 0, "error": 1}
                )
                failure = diagnostics["failures"][0]
                self.assertEqual(failure["instance_id"], "slow[0]")
                self.assertEqual(failure["outcome"], "error")
                self.assertEqual(failure["duration_ms"], 2_000)
                self.assertEqual(failure["timeout_seconds"], 2)
                self.assertEqual(failure["details"]["code"], "timeout")

                # JUnit XML renders the error with the details payload.
                status, content_type, data = server.request(
                    "GET", "/v1/runs/r1/junit.xml"
                )
                self.assertEqual(status, 200)
                xml = data.decode("utf-8")
                self.assertIn('errors="1"', xml)
                self.assertIn('"code":"timeout"', xml)

                # Cross-run aggregation counts the error.
                status, aggregate = server.json(
                    "POST", "/v1/reports/aggregate", {"run_ids": ["r1"]}
                )
                self.assertEqual(status, 200)
                self.assertEqual(aggregate["summary"]["error"], 1)
                self.assertEqual(aggregate["summary"]["duration_ms"], 2_000)
                self.assertFalse(aggregate["passed"])

                # Default retry picks the error outcome up.
                status, retry = server.json(
                    "POST", "/v1/runs/r1/retry", {"id": "r2"}
                )
                self.assertEqual(status, 201)
                self.assertEqual(
                    [i["instance_id"] for i in retry["instances"]], ["slow[0]"]
                )
                self.assertEqual(retry["summary"]["pending"], 1)

    def test_timeout_errors_do_not_mutate_state(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            _, body = claim(server, "r1")
            claim_id = body["claims"][0]["claim_id"]

            status, error = timeouts(server, "ghost")
            self.assertEqual(status, 404)
            self.assertEqual(error["error"]["code"], "run_not_found")

            for bad in (["x"], "nope", 1, None, {"worker_id": "w"}, {"x": 1}):
                status, error = timeouts(server, "r1", bad)
                self.assertEqual(status, 400, bad)
                self.assertEqual(error["error"]["code"], "validation_error", bad)

            status, error = server.json(
                "POST", "/v1/runs/r1/timeouts", raw_body=b"{broken"
            )
            self.assertEqual(status, 400)
            self.assertEqual(error["error"]["code"], "invalid_json")

            # The lease survived every failed call and still redeems.
            status, report = server.json(
                "POST",
                "/v1/runs/r1/results",
                {
                    "instance_id": "c1[0]",
                    "outcome": "passed",
                    "duration_ms": 1,
                    "claim_id": claim_id,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["passed"], 1)

            # Completed runs reject the scan.
            server.json("POST", "/v1/runs/r1/complete")
            status, error = timeouts(server, "r1")
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "run_completed")

    def test_frozen_timeout_survives_catalog_changes(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            service = Service()
            with ServerHarness(service) as server:
                seed_cases(server)
                server.json(
                    "POST",
                    "/v1/cases",
                    case_payload("slow", timeout_seconds=3),
                )
                server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["slow"]})
                # The case is deleted after the run froze its context.
                status, _, _ = server.request("DELETE", "/v1/cases/slow")
                self.assertEqual(status, 204)
                claim(server, "r1", worker_id="w")

                clock["now"] += 3_000
                status, body = timeouts(server, "r1")
                self.assertEqual(status, 200)
                self.assertEqual(body["timed_out"], ["slow[0]"])
                instance = body["run"]["instances"][0]
                self.assertEqual(instance["details"]["timeout_seconds"], 3)
                self.assertEqual(instance["duration_ms"], 3_000)


if __name__ == "__main__":
    unittest.main()
