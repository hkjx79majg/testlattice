"""End-to-end HTTP tests for heartbeat-based liveness detection (hangs)."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import testlattice.service as service_module
from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases


def claim(server: ServerHarness, run_id: str, **body: object):
    payload = {"worker_id": "w1", "lease_seconds": 60}
    payload.update(body)
    return server.json("POST", f"/v1/runs/{run_id}/claims", payload)


def heartbeat(server: ServerHarness, run_id: str, claim_id: str, worker_id: str = "w1"):
    return server.json(
        "POST",
        f"/v1/runs/{run_id}/heartbeats",
        {"claim_id": claim_id, "worker_id": worker_id},
    )


def hangs(server: ServerHarness, run_id: str, body: object = {}):
    return server.json("POST", f"/v1/runs/{run_id}/hangs", body)


class HeartbeatClaimTest(unittest.TestCase):
    def test_claim_without_heartbeat_is_unchanged(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            _, body = claim(server, "r1", lease_seconds=60)
            entry = body["claims"][0]
            self.assertNotIn("heartbeat_seconds", entry)
            self.assertNotIn("last_heartbeat_at", entry)
            self.assertNotIn("heartbeat_deadline_at", entry)

    def test_claim_with_heartbeat_sets_deadline_fields(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            with ServerHarness() as server:
                seed_cases(server)
                server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
                _, body = claim(
                    server, "r1", lease_seconds=60, heartbeat_seconds=10
                )
                entry = body["claims"][0]
                self.assertEqual(entry["last_heartbeat_at"], clock["now"])
                self.assertEqual(
                    entry["heartbeat_deadline_at"], clock["now"] + 10_000
                )
                self.assertEqual(entry["expires_at"], clock["now"] + 60_000)
                self.assertLess(entry["heartbeat_deadline_at"], entry["expires_at"])

    def test_heartbeat_seconds_validation(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            bad = [0, -1, 301, 1.5, "5", True, False, None, [5], {}]
            for value in bad:
                status, error = claim(
                    server, "r1", lease_seconds=60, heartbeat_seconds=value
                )
                self.assertEqual(status, 400, value)
                self.assertEqual(error["error"]["code"], "validation_error", value)
            # Must be strictly smaller than lease_seconds (equal is invalid).
            for value in (10, 11):
                status, error = claim(
                    server, "r1", lease_seconds=10, heartbeat_seconds=value
                )
                self.assertEqual(status, 400)
                self.assertEqual(error["error"]["code"], "validation_error")
            # Unknown field spelling still rejected.
            status, error = claim(server, "r1", heartbeat_sec=5)
            self.assertEqual(status, 400)
            self.assertEqual(error["error"]["code"], "validation_error")
            # Bounds are inclusive.
            status, _ = claim(server, "r1", lease_seconds=2, heartbeat_seconds=1)
            self.assertEqual(status, 200)
            status, _ = claim(server, "r1", lease_seconds=301, heartbeat_seconds=300)
            self.assertEqual(status, 200)

    def test_failed_validation_leaves_no_lease(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            claim(server, "r1", lease_seconds=10, heartbeat_seconds=10)
            _, body = claim(server, "r1", max_items=10)
            # The rejected claim left c1[0] pending and claimable.
            self.assertEqual([c["instance_id"] for c in body["claims"]], ["c1[0]"])


class HeartbeatTest(unittest.TestCase):
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
        server.json(
            "POST",
            "/v1/cases",
            case_payload("fast", timeout_seconds=20),
        )
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["fast", "c1"]})

    def claim_one(self, **body: object):
        status, payload = claim(self.harness, "r1", **body)
        assert status == 200, payload
        return payload["claims"][0]

    def test_heartbeat_renews_deadline_but_not_hard_expiry(self) -> None:
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        self.clock["now"] += 5_000
        status, renewed = heartbeat(self.harness, "r1", entry["claim_id"])
        self.assertEqual(status, 200)
        self.assertEqual(renewed["claim_id"], entry["claim_id"])
        self.assertEqual(renewed["last_heartbeat_at"], self.clock["now"])
        self.assertEqual(
            renewed["heartbeat_deadline_at"], self.clock["now"] + 10_000
        )
        # expires_at is frozen at claim time.
        self.assertEqual(renewed["expires_at"], entry["expires_at"])

    def test_heartbeat_deadline_capped_at_expires_at(self) -> None:
        entry = self.claim_one(lease_seconds=10, heartbeat_seconds=8)
        self.assertEqual(
            entry["heartbeat_deadline_at"], entry["expires_at"] - 2_000
        )
        # Renew 7s in (still before the 8s deadline): now+8s would run past
        # the hard expiry, so the deadline clamps to expires_at.
        self.clock["now"] += 7_000
        status, renewed = heartbeat(self.harness, "r1", entry["claim_id"])
        self.assertEqual(status, 200)
        self.assertEqual(renewed["heartbeat_deadline_at"], renewed["expires_at"])

    def test_unknown_consumed_and_expired_claims_are_not_active(self) -> None:
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=10)

        status, error = heartbeat(self.harness, "r1", "no-such-claim")
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")

        # Consumed claim.
        self.harness.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": entry["instance_id"],
                "outcome": "passed",
                "duration_ms": 1,
                "claim_id": entry["claim_id"],
            },
        )
        status, error = heartbeat(self.harness, "r1", entry["claim_id"])
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")

        # Normally (hard) expired claim: lease vanishes, id is unknown.
        other = self.claim_one(lease_seconds=5, heartbeat_seconds=1)
        self.clock["now"] += 5_000
        status, error = heartbeat(self.harness, "r1", other["claim_id"])
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")
        # Instance became claimable again.
        status, payload = claim(self.harness, "r1", max_items=10)
        self.assertEqual(status, 200)
        self.assertIn(other["instance_id"], [c["instance_id"] for c in payload["claims"]])

    def test_worker_mismatch_is_conflict(self) -> None:
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        status, error = heartbeat(
            self.harness, "r1", entry["claim_id"], worker_id="intruder"
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_conflict")
        # Nothing moved.
        self.clock["now"] += 1_000
        status, renewed = heartbeat(self.harness, "r1", entry["claim_id"])
        self.assertEqual(status, 200)
        self.assertEqual(renewed["last_heartbeat_at"], self.clock["now"])

    def test_claim_without_heartbeat_rejects_heartbeats(self) -> None:
        entry = self.claim_one(lease_seconds=60)
        status, error = heartbeat(self.harness, "r1", entry["claim_id"])
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_not_enabled")

    def test_heartbeat_after_deadline_expires(self) -> None:
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        # One ms before deadline: fine.
        self.clock["now"] += 9_999
        status, _ = heartbeat(self.harness, "r1", entry["claim_id"])
        self.assertEqual(status, 200)
        # New deadline is 10s after the renewal; fail to report in time.
        self.clock["now"] += 10_000
        status, error = heartbeat(self.harness, "r1", entry["claim_id"])
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_expired")

    def test_overdue_instance_stays_pending_and_non_reclaimable(self) -> None:
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        self.clock["now"] += 10_000

        # Heartbeats no longer accepted.
        status, error = heartbeat(self.harness, "r1", entry["claim_id"])
        self.assertEqual(error["error"]["code"], "heartbeat_expired")

        # A late result carrying the claim id is rejected as heartbeat_expired.
        status, error = self.harness.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": entry["instance_id"],
                "outcome": "passed",
                "duration_ms": 4,
                "claim_id": entry["claim_id"],
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_expired")

        # No claim id fares no better while the overdue lease is attached.
        status, error = self.harness.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": entry["instance_id"],
                "outcome": "passed",
                "duration_ms": 4,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_expired")

        # Still pending and not handed out again.
        status, payload = claim(self.harness, "r1", max_items=10)
        self.assertEqual(status, 200)
        self.assertNotIn(
            entry["instance_id"], [c["instance_id"] for c in payload["claims"]]
        )
        status, report = self.harness.json("GET", "/v1/runs/r1")
        instance = next(
            i for i in report["instances"] if i["instance_id"] == entry["instance_id"]
        )
        self.assertEqual(instance["outcome"], "pending")

    def test_overdue_claim_id_rejected_on_other_instance(self) -> None:
        overdue = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        other = self.claim_one(lease_seconds=60)
        self.clock["now"] += 10_000
        # Submitting the other (unleased-vs-claimed) instance with the stale
        # claim id is still a heartbeat expiry, not a conflict.
        status, error = self.harness.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": other["instance_id"],
                "outcome": "passed",
                "duration_ms": 1,
                "claim_id": overdue["claim_id"],
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "heartbeat_expired")

    def test_heartbeat_request_validation(self) -> None:
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        bad_payloads = [
            [],
            "x",
            1,
            None,
            {},
            {"worker_id": "w1"},
            {"claim_id": entry["claim_id"]},
            {"claim_id": entry["claim_id"], "worker_id": "w1", "extra": 1},
            {"claim_id": "", "worker_id": "w1"},
            {"claim_id": entry["claim_id"], "worker_id": "   "},
            {"claim_id": 7, "worker_id": "w1"},
        ]
        for payload in bad_payloads:
            status, error = self.harness.json(
                "POST", "/v1/runs/r1/heartbeats", payload
            )
            self.assertEqual(status, 400, payload)
            self.assertEqual(error["error"]["code"], "validation_error", payload)

        # Whitespace-only is syntactically valid but unknown: not active.
        status, error = heartbeat(self.harness, "r1", "   ")
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")

        status, body = self.harness.json(
            "POST", "/v1/runs/r1/heartbeats", raw_body=b"{bad"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        # Run-level errors reuse the standard codes.
        status, error = heartbeat(self.harness, "ghost", entry["claim_id"])
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "run_not_found")


class HangTest(unittest.TestCase):
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
        server.json("POST", "/v1/cases", case_payload("fast", timeout_seconds=20))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["fast", "c1"]})

    def claim_one(self, **body: object):
        status, payload = claim(self.harness, "r1", **body)
        assert status == 200, payload
        return payload["claims"][0]

    def test_no_hangs_when_healthy_or_unleased(self) -> None:
        self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        self.clock["now"] += 5_000
        status, body = hangs(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["hung"], [])
        self.assertEqual(body["run"]["summary"]["pending"], 2)

        # Claims without heartbeat never show up as hung, even past their
        # heartbeat-less age, as long as the hard lease holds.
        self.claim_one(lease_seconds=60)
        self.clock["now"] += 30_000
        status, body = hangs(self.harness, "r1")
        self.assertEqual(body["hung"], [])

    def test_hang_consumes_lease_and_writes_frozen_result(self) -> None:
        entry = self.claim_one(
            worker_id="walker", lease_seconds=60, heartbeat_seconds=10
        )
        # One renewal before the worker vanishes.
        self.clock["now"] += 8_000
        heartbeat(self.harness, "r1", entry["claim_id"], worker_id="walker")
        self.clock["now"] += 10_000

        status, body = hangs(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["hung"], [entry["instance_id"]])

        instance = next(
            i for i in body["run"]["instances"] if i["instance_id"] == entry["instance_id"]
        )
        self.assertEqual(instance["outcome"], "error")
        self.assertEqual(instance["duration_ms"], 18_000)
        self.assertEqual(
            instance["details"],
            {
                "code": "hung",
                "worker_id": "walker",
                "claimed_at": 1_000_000_000_000,
                "last_heartbeat_at": 1_000_000_008_000,
                "heartbeat_deadline_at": 1_000_000_018_000,
                "detected_at": 1_000_000_018_000,
            },
        )
        self.assertNotIn("coverage", instance)

        # Repeated detection is a no-op and the old claim is dead.
        status, body = hangs(self.harness, "r1")
        self.assertEqual(body["hung"], [])
        status, error = heartbeat(self.harness, "r1", entry["claim_id"])
        self.assertEqual(error["error"]["code"], "claim_not_active")
        status, error = self.harness.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": entry["instance_id"],
                "outcome": "passed",
                "duration_ms": 1,
                "claim_id": entry["claim_id"],
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "claim_not_active")

    def test_hung_instances_listed_in_frozen_order(self) -> None:
        first = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        second = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        self.clock["now"] += 10_000
        status, body = hangs(self.harness, "r1")
        self.assertEqual(
            body["hung"], [first["instance_id"], second["instance_id"]]
        )

    def test_simultaneous_hard_timeout_uses_timeout_result(self) -> None:
        # Heartbeat (5s) and frozen hard timeout (20s) both elapsed.
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=5)
        self.clock["now"] += 20_000
        status, body = hangs(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["hung"], [])
        instance = body["run"]["instances"][0]
        self.assertEqual(instance["outcome"], "error")
        self.assertEqual(instance["duration_ms"], 20_000)
        self.assertEqual(instance["details"]["code"], "timeout")
        self.assertEqual(
            instance["details"]["deadline_at"],
            instance["details"]["claimed_at"] + 20_000,
        )

        # The same precedence holds through the existing timeouts entry: an
        # overdue heartbeat is settled as timeout once the hard deadline hits.
        another = self.claim_one(lease_seconds=60, heartbeat_seconds=5)
        self.clock["now"] += 19_999  # overdue heartbeat, hard timeout not yet
        status, body = hangs(self.harness, "r1")
        self.assertEqual(body["hung"], [another["instance_id"]])

    def test_fresh_heartbeat_past_hard_timeout_left_to_timeouts(self) -> None:
        # Heartbeat interval (10s) shorter than hard timeout (20s); keep
        # heartbeats fresh past the hard timeout.
        entry = self.claim_one(lease_seconds=3600, heartbeat_seconds=10)
        self.clock["now"] += 9_000
        heartbeat(self.harness, "r1", entry["claim_id"])
        self.clock["now"] += 9_000
        heartbeat(self.harness, "r1", entry["claim_id"])
        self.clock["now"] += 9_000  # 27s total: hard timeout (20s) elapsed

        # Hangs must not consume a claim with a fresh heartbeat.
        status, body = hangs(self.harness, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["hung"], [])
        status, report = self.harness.json("GET", "/v1/runs/r1")
        self.assertEqual(report["instances"][0]["outcome"], "pending")

        # The timeouts entry settles it with the regular timeout result.
        status, body = self.harness.json("POST", "/v1/runs/r1/timeouts", {})
        self.assertEqual(body["timed_out"], [entry["instance_id"]])
        self.assertEqual(
            body["run"]["instances"][0]["details"]["code"], "timeout"
        )

    def test_timeout_entry_leaves_overdue_hang_alone_until_deadline(self) -> None:
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=5)
        self.clock["now"] += 5_000
        status, body = self.harness.json("POST", "/v1/runs/r1/timeouts", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["timed_out"], [])
        # Still pending/non-reclaimable.
        status, payload = claim(self.harness, "r1", max_items=10)
        self.assertNotIn(
            entry["instance_id"], [c["instance_id"] for c in payload["claims"]]
        )
        # At the hard deadline the regular timeout result is produced.
        self.clock["now"] += 15_000
        status, body = self.harness.json("POST", "/v1/runs/r1/timeouts", {})
        self.assertEqual(body["timed_out"], [entry["instance_id"]])
        instance = body["run"]["instances"][0]
        self.assertEqual(instance["details"]["code"], "timeout")

    def test_hang_releases_resources(self) -> None:
        self.harness.json("POST", "/v1/resource-pools", {"id": "p1", "name": "P", "capacity": 1})
        self.harness.json(
            "POST",
            "/v1/cases",
            case_payload("pooled", resource_requirements={"p1": 1}),
        )
        self.harness.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["pooled"]})
        _, body = self.harness.json(
            "POST",
            "/v1/runs/r2/claims",
            {"worker_id": "w", "lease_seconds": 60, "heartbeat_seconds": 5},
        )
        entry = body["claims"][0]
        self.clock["now"] += 5_000

        # Overdue but not yet detected: capacity stays occupied.
        status, pool = self.harness.json("GET", "/v1/resource-pools/p1")
        self.assertEqual(pool["allocated"], 1)
        status, body = hangs(self.harness, "r2")
        self.assertEqual(body["hung"], [entry["instance_id"]])
        status, pool = self.harness.json("GET", "/v1/resource-pools/p1")
        self.assertEqual(pool["allocated"], 0)

    def test_hang_does_not_auto_complete_and_flows_into_reports(self) -> None:
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        self.clock["now"] += 10_000
        hangs(self.harness, "r1")
        status, error = self.harness.json("POST", "/v1/runs/r1/complete")
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "run_incomplete")

        # Settle the remaining instance, then complete.
        other = self.claim_one(lease_seconds=60)
        self.harness.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": other["instance_id"],
                "outcome": "passed",
                "duration_ms": 2,
                "claim_id": other["claim_id"],
            },
        )
        status, report = self.harness.json("POST", "/v1/runs/r1/complete")
        self.assertEqual(status, 200)
        self.assertEqual(report["summary"]["error"], 1)
        self.assertEqual(report["summary"]["duration_ms"], 10_000 + 2)

        status, diagnostics = self.harness.json("GET", "/v1/runs/r1/diagnostics")
        self.assertEqual(
            [f["details"]["code"] for f in diagnostics["failures"]], ["hung"]
        )
        status, retry = self.harness.json(
            "POST", "/v1/runs/r1/retry", {"id": "r2"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            [i["instance_id"] for i in retry["instances"]], [entry["instance_id"]]
        )

    def test_hang_validation_and_run_errors(self) -> None:
        for payload in ([], "x", 1, None, {"now": 1}):
            status, error = hangs(self.harness, "r1", payload)
            self.assertEqual(status, 400, payload)
            self.assertEqual(error["error"]["code"], "validation_error", payload)

        status, body = self.harness.json(
            "POST", "/v1/runs/r1/hangs", raw_body=b"{bad"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        status, error = hangs(self.harness, "ghost")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "run_not_found")

        self.harness.json("POST", "/v1/runs/r1/results", {
            "instance_id": "fast[0]", "outcome": "passed", "duration_ms": 1,
        })
        self.harness.json("POST", "/v1/runs/r1/results", {
            "instance_id": "c1[0]", "outcome": "passed", "duration_ms": 1,
        })
        self.harness.json("POST", "/v1/runs/r1/complete")
        status, error = hangs(self.harness, "r1")
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "run_completed")

    def test_concurrent_hang_and_submit_only_one_wins(self) -> None:
        entry = self.claim_one(lease_seconds=60, heartbeat_seconds=10)
        self.clock["now"] += 10_000
        outcomes: list[tuple[str, int]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def submit() -> None:
            barrier.wait()
            status, _ = self.harness.json(
                "POST",
                "/v1/runs/r1/results",
                {
                    "instance_id": entry["instance_id"],
                    "outcome": "passed",
                    "duration_ms": 7,
                    "claim_id": entry["claim_id"],
                },
            )
            with lock:
                outcomes.append(("submit", status))

        def hang() -> None:
            barrier.wait()
            status, body = hangs(self.harness, "r1")
            with lock:
                outcomes.append(("hang", status))
                assert body["hung"] == ([entry["instance_id"]] if status == 200 else body["hung"])

        threads = [threading.Thread(target=submit), threading.Thread(target=hang)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        statuses = {name: code for name, code in outcomes}
        # Submit must never succeed once the heartbeat deadline elapsed.
        self.assertEqual(statuses["submit"], 409)
        self.assertEqual(statuses["hang"], 200)
        _, report = self.harness.json("GET", "/v1/runs/r1")
        instance = report["instances"][0]
        self.assertEqual(instance["outcome"], "error")
        self.assertEqual(instance["details"]["code"], "hung")


if __name__ == "__main__":
    unittest.main()
