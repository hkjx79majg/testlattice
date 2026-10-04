"""End-to-end HTTP tests for platform-side timeout detection."""

from __future__ import annotations

import unittest
from unittest import mock

import testlattice.service as service_module
from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases


def timeouts(server: ServerHarness, run_id: str, body: object = {}):
    return server.json("POST", f"/v1/runs/{run_id}/timeouts", body)


class TimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = {"now": 1_000_000_000_000}
        self._patch = mock.patch.object(
            service_module, "_now_ms", lambda: self.clock["now"]
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.service = Service()
        self.harness = ServerHarness(self.service)
        self.harness.__enter__()
        self.addCleanup(self.harness.__exit__, None, None, None)
        server = self.harness
        seed_cases(server)
        server.json("POST", "/v1/cases", case_payload("fast", timeout_seconds=10))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["fast", "c1"]})

    def claim(self, run_id: str = "r1", **body: object):
        payload = {"worker_id": "w1", "lease_seconds": 3600}
        payload.update(body)
        return self.harness.json("POST", f"/v1/runs/{run_id}/claims", payload)

    def test_empty_run_and_unleased_instances_are_not_timed_out(self) -> None:
        status, body = timeouts(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["timed_out"], [])
        self.assertEqual(body["run"]["summary"]["pending"], 2)

    def test_timeout_writes_frozen_error_result(self) -> None:
        _, claims = self.claim(max_items=2)
        by_id = {c["instance_id"]: c for c in claims["claims"]}
        self.assertEqual(set(by_id), {"fast[0]", "c1[0]"})

        # One millisecond before the fast instance's deadline: no hits.
        self.clock["now"] += 10_000 - 1
        status, body = timeouts(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["timed_out"], [])

        # Exactly at claimed_at + timeout_seconds * 1000: fast[0] times out;
        # c1[0] (default 300s) is still within its budget.
        self.clock["now"] += 1
        status, body = timeouts(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["timed_out"], ["fast[0]"])

        run = body["run"]
        self.assertEqual(run["status"], "open")
        instance = next(
            item for item in run["instances"] if item["instance_id"] == "fast[0]"
        )
        self.assertEqual(instance["outcome"], "error")
        self.assertEqual(instance["duration_ms"], 10_000)
        self.assertEqual(
            instance["details"],
            {
                "code": "timeout",
                "timeout_seconds": 10,
                "worker_id": "w1",
                "claimed_at": 1_000_000_000_000,
                "deadline_at": 1_000_000_010_000,
                "detected_at": 1_000_000_010_000,
            },
        )
        self.assertNotIn("coverage", instance)
        summary = run["summary"]
        self.assertEqual(summary["error"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["duration_ms"], 10_000)

        # A repeated check is a no-op: consumed leases are not reprocessed.
        status, body = timeouts(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["timed_out"], [])

    def test_late_submit_with_old_claim_id_conflicts(self) -> None:
        _, claims = self.claim(max_items=1)
        claim_id = claims["claims"][0]["claim_id"]
        self.clock["now"] += 10_000
        status, body = timeouts(self.harness, "r1")
        self.assertEqual(body["timed_out"], ["fast[0]"])

        status, error = self.harness.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": "fast[0]",
                "outcome": "passed",
                "duration_ms": 5,
                "claim_id": claim_id,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")

        # The timeout result is not overwritten.
        _, run = self.harness.json("GET", "/v1/runs/r1")
        instance = run["instances"][0]
        self.assertEqual(instance["outcome"], "error")
        self.assertEqual(instance["duration_ms"], 10_000)

    def test_expired_lease_is_dropped_not_timed_out(self) -> None:
        # Lease (5s) expires before the frozen timeout (10s) elapses.
        _, claims = self.claim(max_items=1, lease_seconds=5)
        self.assertEqual(len(claims["claims"]), 1)
        self.clock["now"] += 20_000
        status, body = timeouts(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["timed_out"], [])
        # The instance stays pending and can be claimed again.
        status, claims = self.claim(max_items=1, worker_id="w2")
        self.assertEqual(status, 200)
        self.assertEqual(
            [c["instance_id"] for c in claims["claims"]], ["fast[0]"]
        )

    def test_instances_with_results_are_skipped(self) -> None:
        _, claims = self.claim(max_items=1)
        claim_id = claims["claims"][0]["claim_id"]
        status, _ = self.harness.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": "fast[0]",
                "outcome": "passed",
                "duration_ms": 3,
                "claim_id": claim_id,
            },
        )
        self.assertEqual(status, 200)
        self.clock["now"] += 10_000
        status, body = timeouts(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["timed_out"], [])

    def test_timeout_result_flows_into_exports_and_retry(self) -> None:
        self.claim(max_items=2)
        self.clock["now"] += 400_000
        status, body = timeouts(self.harness, "r1")
        self.assertEqual(body["timed_out"], ["fast[0]", "c1[0]"])

        status, _ = self.harness.json("POST", "/v1/runs/r1/complete")
        self.assertEqual(status, 200)

        status, diagnostics = self.harness.json("GET", "/v1/runs/r1/diagnostics")
        self.assertEqual(status, 200)
        self.assertEqual(
            diagnostics["summary"], {"total": 2, "failed": 0, "error": 2}
        )
        self.assertEqual(
            [f["details"]["code"] for f in diagnostics["failures"]],
            ["timeout", "timeout"],
        )

        status, _, xml = self.harness.request("GET", "/v1/runs/r1/junit.xml")
        self.assertEqual(status, 200)
        self.assertIn(b'<error message="error">', xml)
        self.assertIn(b'"code":"timeout"', xml)

        status, aggregate = self.harness.json(
            "POST", "/v1/reports/aggregate", {"run_ids": ["r1"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(aggregate["summary"]["error"], 2)
        self.assertEqual(aggregate["summary"]["duration_ms"], 310_000)

        # Timeout errors are retryable under the default outcomes.
        status, retry = self.harness.json(
            "POST", "/v1/runs/r1/retry", {"id": "r2"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(retry["summary"]["total"], 2)
        self.assertEqual(retry["retry_of"], "r1")

    def test_completed_run_rejects_timeout_check(self) -> None:
        self.claim(max_items=2)
        self.clock["now"] += 400_000
        timeouts(self.harness, "r1")
        self.harness.json("POST", "/v1/runs/r1/complete")
        status, error = timeouts(self.harness, "r1")
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "run_completed")

    def test_unknown_run_returns_404(self) -> None:
        status, error = timeouts(self.harness, "nope")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "run_not_found")

    def test_invalid_requests_change_nothing(self) -> None:
        _, claims = self.claim(max_items=1)
        claim_id = claims["claims"][0]["claim_id"]

        status, _, _ = self.harness.request(
            "POST", "/v1/runs/r1/timeouts", raw_body=b"{bad"
        )
        self.assertEqual(status, 400)
        _, body = self.harness.json(
            "POST", "/v1/runs/r1/timeouts", raw_body=b"{bad"
        )
        self.assertEqual(body["error"]["code"], "invalid_json")

        for payload in ([], "x", 1, None, {"now": 1}):
            status, error = timeouts(self.harness, "r1", payload)
            self.assertEqual(status, 400)
            self.assertEqual(error["error"]["code"], "validation_error")

        # The lease is still active and accepts its own result.
        status, _ = self.harness.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": "fast[0]",
                "outcome": "passed",
                "duration_ms": 3,
                "claim_id": claim_id,
            },
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
