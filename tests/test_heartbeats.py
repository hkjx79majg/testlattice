"""End-to-end HTTP tests for heartbeat liveness and hang detection."""

from __future__ import annotations

import unittest
from unittest import mock

import testlattice.service as service_module
from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases

NOW = 1_000_000_000_000


def heartbeats(server: ServerHarness, run_id: str, body: object):
    return server.json("POST", f"/v1/runs/{run_id}/heartbeats", body)


def hangs(server: ServerHarness, run_id: str, body: object = {}):
    return server.json("POST", f"/v1/runs/{run_id}/hangs", body)


class HeartbeatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = {"now": NOW}
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

    def submit(self, instance_id: str, claim_id: str, **extra: object):
        payload = {
            "instance_id": instance_id,
            "outcome": "passed",
            "duration_ms": 3,
            "claim_id": claim_id,
        }
        payload.update(extra)
        return self.harness.json("POST", "/v1/runs/r1/results", payload)

    def test_claim_with_heartbeat_adds_deadline_fields(self) -> None:
        status, body = self.claim(max_items=1, heartbeat_seconds=10)
        self.assertEqual(status, 200)
        first = body["claims"][0]
        self.assertEqual(first["instance_id"], "fast[0]")
        self.assertEqual(first["last_heartbeat_at"], NOW)
        self.assertEqual(first["heartbeat_deadline_at"], NOW + 10_000)
        self.assertEqual(first["expires_at"], NOW + 3_600_000)

    def test_claim_without_heartbeat_is_unchanged(self) -> None:
        status, body = self.claim(max_items=1)
        self.assertEqual(status, 200)
        first = body["claims"][0]
        self.assertNotIn("last_heartbeat_at", first)
        self.assertNotIn("heartbeat_deadline_at", first)

    def test_heartbeat_deadline_never_past_expiry(self) -> None:
        # heartbeat_seconds < lease_seconds, so the initial deadline is
        # exactly one interval out and always strictly before expires_at.
        status, body = self.claim(max_items=1, lease_seconds=15, heartbeat_seconds=10)
        self.assertEqual(status, 200)
        first = body["claims"][0]
        self.assertEqual(first["heartbeat_deadline_at"], NOW + 10_000)
        self.assertEqual(first["expires_at"], NOW + 15_000)

    def test_heartbeat_seconds_validation(self) -> None:
        for value in (True, False, "10", 1.5, None, 0, -1, 301):
            status, error = self.claim(max_items=1, heartbeat_seconds=value)
            self.assertEqual(status, 400, value)
            self.assertEqual(error["error"]["code"], "validation_error")
        # 1 and 300 are in range but must stay below lease_seconds.
        for lease, heartbeat in ((3600, 3600), (60, 60), (60, 61), (1, 1)):
            status, error = self.claim(
                max_items=1, lease_seconds=lease, heartbeat_seconds=heartbeat
            )
            self.assertEqual(status, 400, (lease, heartbeat))
            self.assertEqual(error["error"]["code"], "validation_error")
        # Boundary values are accepted when below the lease.
        status, body = self.claim(max_items=1, lease_seconds=301, heartbeat_seconds=300)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["claims"]), 1)
        # Nothing was leased by the failed requests above.
        status, body = self.claim(max_items=1, worker_id="w2")
        self.assertEqual([c["instance_id"] for c in body["claims"]], ["c1[0]"])

    def test_heartbeat_refreshes_deadline_without_extending_lease(self) -> None:
        _, claims = self.claim(max_items=1, heartbeat_seconds=10)
        claim_id = claims["claims"][0]["claim_id"]

        self.clock["now"] += 4_000
        status, body = heartbeats(
            self.harness, "r1", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["claim_id"], claim_id)
        self.assertEqual(body["instance_id"], "fast[0]")
        self.assertEqual(body["last_heartbeat_at"], NOW + 4_000)
        self.assertEqual(body["heartbeat_deadline_at"], NOW + 14_000)
        self.assertEqual(body["expires_at"], NOW + 3_600_000)

        # One millisecond before the new deadline: still accepted.
        self.clock["now"] = NOW + 13_999
        status, body = heartbeats(
            self.harness, "r1", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["last_heartbeat_at"], NOW + 13_999)
        self.assertEqual(body["heartbeat_deadline_at"], NOW + 23_999)

        # The lease itself is untouched: expiry and hard timeout are fixed.
        _, run = self.harness.json("GET", "/v1/runs/r1")
        self.assertEqual(run["summary"]["pending"], 2)

    def test_heartbeat_deadline_clamped_to_expiry_on_refresh(self) -> None:
        _, claims = self.claim(max_items=1, lease_seconds=15, heartbeat_seconds=10)
        claim_id = claims["claims"][0]["claim_id"]
        self.clock["now"] += 9_000
        status, body = heartbeats(
            self.harness, "r1", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["heartbeat_deadline_at"], NOW + 15_000)

    def test_heartbeat_errors(self) -> None:
        _, claims = self.claim(max_items=2, heartbeat_seconds=10)
        by_id = {c["instance_id"]: c for c in claims["claims"]}
        fast_claim = by_id["fast[0]"]["claim_id"]

        # Unknown claim id.
        status, error = heartbeats(
            self.harness, "r1", {"claim_id": "nope", "worker_id": "w1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")

        # Worker mismatch.
        status, error = heartbeats(
            self.harness, "r1", {"claim_id": fast_claim, "worker_id": "w2"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_conflict")

        # Validation failures leave the lease usable.
        for payload in (
            [],
            "x",
            1,
            None,
            {},
            {"claim_id": fast_claim},
            {"worker_id": "w1"},
            {"claim_id": fast_claim, "worker_id": "w1", "extra": 1},
            {"claim_id": "", "worker_id": "w1"},
            {"claim_id": fast_claim, "worker_id": "  "},
        ):
            status, error = heartbeats(self.harness, "r1", payload)
            self.assertEqual(status, 400, payload)
            self.assertEqual(error["error"]["code"], "validation_error")
        status, _, _ = self.harness.request(
            "POST", "/v1/runs/r1/heartbeats", raw_body=b"{bad"
        )
        self.assertEqual(status, 400)

        # Consumed claims are no longer active.
        status, _ = self.submit("fast[0]", fast_claim)
        self.assertEqual(status, 200)
        status, error = heartbeats(
            self.harness, "r1", {"claim_id": fast_claim, "worker_id": "w1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")

    def test_heartbeat_not_enabled(self) -> None:
        _, claims = self.claim(max_items=1)
        claim_id = claims["claims"][0]["claim_id"]
        status, error = heartbeats(
            self.harness, "r1", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_not_enabled")

    def test_heartbeat_expired_blocks_heartbeat_and_submit(self) -> None:
        _, claims = self.claim(max_items=1, heartbeat_seconds=10)
        claim_id = claims["claims"][0]["claim_id"]

        self.clock["now"] += 10_000
        status, error = heartbeats(
            self.harness, "r1", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_expired")

        status, error = self.submit("fast[0]", claim_id)
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_expired")

        # The instance stays pending and cannot be re-claimed, even after the
        # lease's own expiry has passed: the stuck lease is never dropped.
        self.clock["now"] += 4_000_000
        _, body = self.claim(max_items=2, worker_id="w2")
        self.assertEqual(
            [c["instance_id"] for c in body["claims"]], ["c1[0]"]
        )
        _, run = self.harness.json("GET", "/v1/runs/r1")
        instance = run["instances"][0]
        self.assertEqual(instance["instance_id"], "fast[0]")
        self.assertEqual(instance["outcome"], "pending")
        status, error = heartbeats(
            self.harness, "r1", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_expired")
        status, error = self.submit("fast[0]", claim_id)
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_expired")

    def test_heartbeat_expired_keeps_pool_allocation_until_hang(self) -> None:
        server = self.harness
        server.json(
            "POST",
            "/v1/resource-pools",
            {"id": "gpu", "name": "GPU", "capacity": 1},
        )
        server.json(
            "POST",
            "/v1/cases",
            case_payload("heavy", timeout_seconds=5000, resource_requirements={"gpu": 1}),
        )
        server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["heavy"]})
        _, claims = self.claim(
            "r2", max_items=1, lease_seconds=3600, heartbeat_seconds=10
        )
        claim_id = claims["claims"][0]["claim_id"]

        # Past the heartbeat deadline and the lease expiry, but not the
        # 5000-second hard timeout: the stuck lease keeps its allocation.
        self.clock["now"] += 4_000_000
        _, pool = server.json("GET", "/v1/resource-pools/gpu")
        self.assertEqual(pool["allocated"], 1)
        self.assertEqual(pool["available"], 0)

        status, body = hangs(server, "r2")
        self.assertEqual(status, 200)
        self.assertEqual(body["hung"], ["heavy[0]"])
        _, pool = server.json("GET", "/v1/resource-pools/gpu")
        self.assertEqual(pool["allocated"], 0)

        # The reaped claim is gone for good.
        status, error = heartbeats(
            server, "r2", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")

    def test_hangs_writes_frozen_error_result(self) -> None:
        _, claims = self.claim(max_items=2, heartbeat_seconds=10)
        self.assertEqual(len(claims["claims"]), 2)

        # One millisecond before the deadline: no hits.
        self.clock["now"] += 10_000 - 1
        status, body = hangs(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["hung"], [])

        # At the deadline: c1[0] (timeout 300s) hangs; fast[0] reached its
        # hard timeout at the same instant and gets the timeout result.
        self.clock["now"] += 1
        status, body = hangs(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["hung"], ["c1[0]"])

        run = body["run"]
        self.assertEqual(run["status"], "open")
        by_id = {i["instance_id"]: i for i in run["instances"]}
        hung_instance = by_id["c1[0]"]
        self.assertEqual(hung_instance["outcome"], "error")
        self.assertEqual(hung_instance["duration_ms"], 10_000)
        self.assertEqual(
            hung_instance["details"],
            {
                "code": "hung",
                "worker_id": "w1",
                "claimed_at": NOW,
                "last_heartbeat_at": NOW,
                "heartbeat_deadline_at": NOW + 10_000,
                "detected_at": NOW + 10_000,
            },
        )
        timed_out = by_id["fast[0]"]
        self.assertEqual(timed_out["outcome"], "error")
        self.assertEqual(timed_out["duration_ms"], 10_000)
        self.assertEqual(timed_out["details"]["code"], "timeout")
        self.assertEqual(timed_out["details"]["deadline_at"], NOW + 10_000)
        summary = run["summary"]
        self.assertEqual(summary["error"], 2)
        self.assertEqual(summary["pending"], 0)

        # A repeated check is a no-op: consumed leases are not reprocessed.
        status, body = hangs(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["hung"], [])

    def test_hangs_releases_resources_and_allows_completion(self) -> None:
        self.claim(max_items=2, heartbeat_seconds=10)
        self.clock["now"] += 10_000
        hangs(self.harness, "r1")
        status, run = self.harness.json("POST", "/v1/runs/r1/complete")
        self.assertEqual(status, 200)
        self.assertEqual(run["status"], "completed")
        self.assertFalse(run["passed"])

        # Hung results flow into the existing exports unchanged.
        status, diagnostics = self.harness.json("GET", "/v1/runs/r1/diagnostics")
        self.assertEqual(status, 200)
        codes = {f["instance_id"]: f["details"]["code"] for f in diagnostics["failures"]}
        self.assertEqual(codes, {"fast[0]": "timeout", "c1[0]": "hung"})

    def test_hangs_ignores_instances_without_heartbeat(self) -> None:
        # Plain leases expire silently and are never hung.
        self.claim(max_items=2, lease_seconds=5)
        self.clock["now"] += 20_000
        status, body = hangs(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["hung"], [])
        self.assertEqual(body["run"]["summary"]["pending"], 2)

    def test_only_one_transition_wins(self) -> None:
        _, claims = self.claim(max_items=1, heartbeat_seconds=10)
        claim_id = claims["claims"][0]["claim_id"]

        # Submit first: the later hang check sees no live lease.
        status, _ = self.submit("fast[0]", claim_id)
        self.assertEqual(status, 200)
        self.clock["now"] += 10_000
        status, body = hangs(self.harness, "r1")
        self.assertEqual(body["hung"], [])

        # Hang first on the other instance: the late submit then fails.
        _, claims = self.claim(max_items=1, heartbeat_seconds=10)
        claim_id = claims["claims"][0]["claim_id"]
        self.assertEqual(claims["claims"][0]["instance_id"], "c1[0]")
        self.clock["now"] += 10_000
        status, body = hangs(self.harness, "r1")
        self.assertEqual(body["hung"], ["c1[0]"])
        status, error = self.submit("c1[0]", claim_id)
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")
        _, run = self.harness.json("GET", "/v1/runs/r1")
        instance = run["instances"][1]
        self.assertEqual(instance["outcome"], "error")
        self.assertEqual(instance["details"]["code"], "hung")

    def test_hangs_run_errors_and_invalid_requests(self) -> None:
        _, claims = self.claim(max_items=1, heartbeat_seconds=10)
        claim_id = claims["claims"][0]["claim_id"]

        status, error = hangs(self.harness, "nope")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "run_not_found")
        status, error = heartbeats(
            self.harness, "nope", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "run_not_found")

        for payload in ([], "x", 1, None, {"now": 1}):
            status, error = hangs(self.harness, "r1", payload)
            self.assertEqual(status, 400)
            self.assertEqual(error["error"]["code"], "validation_error")
        status, _, _ = self.harness.request(
            "POST", "/v1/runs/r1/hangs", raw_body=b"{bad"
        )
        self.assertEqual(status, 400)

        # Failed requests changed nothing: the lease is still live.
        status, body = heartbeats(
            self.harness, "r1", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 200)

        # Completed runs reject both endpoints.
        self.submit("fast[0]", claim_id)
        _, claims = self.claim(max_items=1)
        self.submit("c1[0]", claims["claims"][0]["claim_id"])
        self.harness.json("POST", "/v1/runs/r1/complete")
        status, error = hangs(self.harness, "r1")
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "run_completed")
        status, error = heartbeats(
            self.harness, "r1", {"claim_id": claim_id, "worker_id": "w1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "run_completed")


if __name__ == "__main__":
    unittest.main()
