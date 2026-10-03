"""End-to-end HTTP tests for lease-based parallel scheduling."""

from __future__ import annotations

import threading
import unittest

from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases


class MutableClock:
    def __init__(self, now_ms: int = 1_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class ClockServerHarness(ServerHarness):
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        service = Service(clock=clock)
        super().__init__(service)


def claim(server: ServerHarness, run_id: str, body: object = ..., **fields: object):
    if body is ...:
        body = {"worker_id": "w1", "lease_seconds": 60}
        body.update(fields)
    return server.json("POST", f"/v1/runs/{run_id}/claims", body)


def submit(server: ServerHarness, run_id: str, instance_id: str, **extra: object):
    payload = {"instance_id": instance_id, "outcome": "passed", "duration_ms": 5}
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def seed_plan_server() -> tuple[ClockServerHarness, MutableClock]:
    clock = MutableClock()
    server = ClockServerHarness(clock)
    server.__enter__()
    server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
    server.json(
        "POST",
        "/v1/fixtures",
        {
            "id": "f1",
            "name": "F1",
            "setup_steps": [{"action": "boot"}],
            "teardown_steps": [{"action": "halt"}],
        },
    )
    server.json(
        "POST",
        "/v1/cases",
        case_payload("c1", fixture_ids=["f1"], kind="api", timeout_seconds=42),
    )
    server.json(
        "POST",
        "/v1/cases",
        case_payload("c2", parameterization={"axes": {"x": [1, 2]}}),
    )
    return server, clock


class ClaimTest(unittest.TestCase):
    def test_claim_returns_frozen_context_in_frozen_order(self) -> None:
        server, clock = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})

        status, body = claim(server, "r1", max_items=3, lease_seconds=10)
        self.assertEqual(status, 200)
        claims = body["claims"]
        self.assertEqual([c["instance_id"] for c in claims], ["c2[0]", "c2[1]", "c1[0]"])

        first = claims[0]
        self.assertIsInstance(first["claim_id"], str)
        self.assertTrue(first["claim_id"])
        self.assertEqual(first["case_id"], "c2")
        self.assertEqual(first["case_name"], "name-c2")
        self.assertEqual(first["kind"], "unit")
        self.assertEqual(first["parameters"], {"x": 1})
        self.assertEqual(first["timeout_seconds"], 300)
        self.assertEqual(first["setup"], [])
        self.assertEqual(first["steps"], [{"action": "run"}])
        self.assertEqual(first["teardown"], [])
        self.assertIsInstance(first["expires_at"], int)
        self.assertEqual(first["expires_at"], clock.now_ms + 10_000)

        # The fixture-bearing case surfaces its frozen setup/teardown plan.
        third = claims[2]
        self.assertEqual(third["case_id"], "c1")
        self.assertEqual(third["kind"], "api")
        self.assertEqual(third["timeout_seconds"], 42)
        self.assertEqual(
            third["setup"], [{"fixture_id": "f1", "steps": [{"action": "boot"}]}]
        )
        self.assertEqual(
            third["teardown"], [{"fixture_id": "f1", "steps": [{"action": "halt"}]}]
        )

        # Claim ids are unique and opaque: 32 hex chars with no structural
        # tie to instance or worker ids.
        ids = [c["claim_id"] for c in claims]
        self.assertEqual(len(set(ids)), 3)
        for claim_id in ids:
            self.assertRegex(claim_id, r"^[0-9a-f]{32}$")
            self.assertNotIn(claim_id, ("c1[0]", "c2[0]", "c2[1]", "w1"))

    def test_report_carries_no_lease_fields(self) -> None:
        server, _ = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
        claim(server, "r1", max_items=2)
        status, report = server.json("GET", "/v1/runs/r1")
        self.assertEqual(status, 200)
        for instance in report["instances"]:
            self.assertEqual(
                set(instance), {"instance_id", "case_id", "case_name", "parameters", "outcome"}
            )
            self.assertEqual(instance["outcome"], "pending")
        self.assertEqual(report["summary"]["pending"], 3)
        self.assertEqual(report["summary"]["duration_ms"], 0)

    def test_defaults_max_items_one_and_worker_trimmed(self) -> None:
        server, _ = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})

        status, body = claim(
            server, "r1", {"worker_id": "  worker-7  ", "lease_seconds": 5}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["claims"]), 1)

        # The leased instance is skipped by the next claim; nothing is left
        # but the second instance once max_items covers both.
        status, body = claim(server, "r1", {"worker_id": "w2", "max_items": 2, "lease_seconds": 5})
        self.assertEqual(status, 200)
        self.assertEqual([c["instance_id"] for c in body["claims"]], ["c2[1]"])

    def test_empty_claims_when_none_available(self) -> None:
        server, _ = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
        status, body = claim(server, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["claims"]), 1)
        status, body = claim(server, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"claims": []})

    def test_expired_lease_releases_pending_instance_without_result(self) -> None:
        server, clock = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})

        status, body = claim(server, "r1", {"worker_id": "w1", "lease_seconds": 10})
        self.assertEqual(status, 200)
        leased = body["claims"][0]["claim_id"]
        first_expiry = body["claims"][0]["expires_at"]

        # One millisecond before expiry the lease is still live.
        clock.now_ms = first_expiry - 1
        status, body = claim(server, "r1", {"worker_id": "w2", "max_items": 2, "lease_seconds": 10})
        self.assertEqual([c["instance_id"] for c in body["claims"]], ["c2[1]"])

        # Reaching expires_at invalidates the lease; the instance is pending
        # with no result and can be claimed again with a fresh token.
        clock.now_ms = first_expiry
        status, body = claim(server, "r1", {"worker_id": "w2", "max_items": 2, "lease_seconds": 10})
        self.assertEqual(status, 200)
        again = body["claims"]
        self.assertEqual([c["instance_id"] for c in again], ["c2[0]"])
        self.assertNotEqual(again[0]["claim_id"], leased)
        self.assertEqual(again[0]["expires_at"], first_expiry + 10_000)

        _, report = server.json("GET", "/v1/runs/r1")
        self.assertEqual(report["summary"]["pending"], 2)
        self.assertEqual(report["summary"]["duration_ms"], 0)

    def test_submit_with_matching_claim_consumes_lease(self) -> None:
        server, clock = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})

        _, body = claim(server, "r1", max_items=3)
        by_instance = {c["instance_id"]: c["claim_id"] for c in body["claims"]}
        expiry = max(c["expires_at"] for c in body["claims"])

        status, report = submit(
            server, "r1", "c2[0]", claim_id=by_instance["c2[0]"], duration_ms=9
        )
        self.assertEqual(status, 200)
        self.assertEqual(report["instances"][0]["outcome"], "passed")
        self.assertEqual(report["summary"]["pending"], 2)
        self.assertEqual(report["summary"]["duration_ms"], 9)

        # The consumed token cannot submit again (even for the same instance).
        status, body = submit(server, "r1", "c2[0]", claim_id=by_instance["c2[0]"])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "claim_not_active")

        # The two unfinished instances are still leased, so nothing is offered.
        status, body = claim(
            server, "r1", {"worker_id": "wx", "max_items": 3, "lease_seconds": 5}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["claims"], [])

        # After those leases expire the finished instance is never re-leased.
        clock.now_ms = expiry
        status, body = claim(
            server, "r1", {"worker_id": "wx", "max_items": 3, "lease_seconds": 5}
        )
        reclaimed = {c["instance_id"]: c["claim_id"] for c in body["claims"]}
        self.assertEqual(sorted(reclaimed), ["c1[0]", "c2[1]"])

        # The old tokens expired and can no longer submit.
        status, err = submit(server, "r1", "c2[1]", claim_id=by_instance["c2[1]"])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "claim_not_active")

        # The new leases remain usable independently.
        status, _ = submit(server, "r1", "c2[1]", claim_id=reclaimed["c2[1]"])
        self.assertEqual(status, 200)
        status, _ = submit(server, "r1", "c1[0]", claim_id=reclaimed["c1[0]"])
        self.assertEqual(status, 200)
        status, completed = server.json("POST", "/v1/runs/r1/complete")
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "completed")

    def test_claim_conflict_variants(self) -> None:
        server, clock = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})

        _, body = claim(server, "r1", {"worker_id": "w1", "max_items": 2, "lease_seconds": 60})
        ids = [c["claim_id"] for c in body["claims"]]

        # No claim_id while the instance is leased.
        status, err = submit(server, "r1", "c2[0]")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "claim_conflict")

        # Another instance's live lease.
        status, err = submit(server, "r1", "c2[0]", claim_id=ids[1])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "claim_conflict")

        # The genuine holder still can submit; conflicts must not have
        # consumed either lease or written any result.
        status, _ = submit(server, "r1", "c2[0]", claim_id=ids[0])
        self.assertEqual(status, 200)
        _, report = server.json("GET", "/v1/runs/r1")
        self.assertEqual(report["summary"]["pending"], 1)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertEqual(clock.now_ms, 1_000_000)  # clock untouched by failures

    def test_claim_not_active_variants(self) -> None:
        server, clock = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})

        _, body = claim(server, "r1", {"worker_id": "w1", "max_items": 3, "lease_seconds": 10})
        ids = [c["claim_id"] for c in body["claims"]]
        expiries = [c["expires_at"] for c in body["claims"]]

        # Unknown token.
        status, err = submit(server, "r1", "c2[0]", claim_id="nope")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "claim_not_active")

        # Expired token that still belongs to this instance.
        clock.now_ms = expiries[0]
        status, err = submit(server, "r1", "c2[0]", claim_id=ids[0])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "claim_not_active")

        # Once expired, direct (claim-less) submission keeps legacy behavior.
        status, _ = submit(server, "r1", "c2[0]")
        self.assertEqual(status, 200)

        # A consumed token from a finished instance is not active.
        submit(server, "r1", "c2[1]", claim_id=ids[1])
        status, err = submit(server, "r1", "c2[1]", claim_id=ids[1])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "claim_not_active")

        # An expired token belonging to another instance is not_active rather
        # than conflict.
        clock.now_ms = expiries[2]
        status, err = submit(server, "r1", "c1[0]", claim_id=ids[0])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "claim_not_active")

    def test_failed_submit_writes_neither_result_nor_coverage(self) -> None:
        server, _ = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
        _, body = claim(server, "r1")
        claim_id = body["claims"][0]["claim_id"]

        coverage = {"files": {"a.py": {"executable_lines": [1], "covered_lines": [1]}}}
        status, err = submit(
            server,
            "r1",
            "c1[0]",
            claim_id="ghost",
            coverage=coverage,
        )
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "claim_not_active")

        _, report = server.json("GET", "/v1/runs/r1")
        self.assertEqual(report["instances"][0]["outcome"], "pending")
        self.assertNotIn("duration_ms", report["instances"][0])

        # Lease survived the rejected submission and still works.
        status, _ = submit(server, "r1", "c1[0]", claim_id=claim_id, coverage=coverage)
        self.assertEqual(status, 200)
        server.json("POST", "/v1/runs/r1/complete")
        status, cov = server.json("GET", "/v1/runs/r1/coverage")
        self.assertEqual(status, 200)
        self.assertEqual(cov["summary"]["covered_lines"], 1)

    def test_direct_submission_still_works_without_any_lease(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, report = submit(server, "r1", "c1[0]")
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["passed"], 1)

    def test_claim_validation_errors(self) -> None:
        server, _ = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

        bad_payloads = [
            ["w1"],
            "w1",
            123,
            None,
            {},
            {"lease_seconds": 10},
            {"worker_id": "w1"},
            {"worker_id": "", "lease_seconds": 10},
            {"worker_id": "   ", "lease_seconds": 10},
            {"worker_id": 7, "lease_seconds": 10},
            {"worker_id": "w1", "lease_seconds": 0},
            {"worker_id": "w1", "lease_seconds": 3601},
            {"worker_id": "w1", "lease_seconds": 1.5},
            {"worker_id": "w1", "lease_seconds": True},
            {"worker_id": "w1", "lease_seconds": None},
            {"worker_id": "w1", "lease_seconds": "10"},
            {"worker_id": "w1", "max_items": 0, "lease_seconds": 10},
            {"worker_id": "w1", "max_items": 101, "lease_seconds": 10},
            {"worker_id": "w1", "max_items": 1.5, "lease_seconds": 10},
            {"worker_id": "w1", "max_items": True, "lease_seconds": 10},
            {"worker_id": "w1", "max_items": "5", "lease_seconds": 10},
            {"worker_id": "w1", "lease_seconds": 10, "extra": 1},
        ]
        for payload in bad_payloads:
            status, body = server.json("POST", "/v1/runs/r1/claims", payload)
            self.assertEqual(status, 400, payload)
            self.assertEqual(body["error"]["code"], "validation_error", payload)

        # max_items null falls back to the default of 1.
        status, body = claim(
            server, "r1", {"worker_id": "w1", "max_items": None, "lease_seconds": 10}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["claims"]), 1)

        # Malformed JSON.
        status, body = server.json(
            "POST", "/v1/runs/r1/claims", raw_body=b"{broken"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        # claim_id type errors on results are validation errors.
        status, body = submit(server, "r1", "c1[0]", claim_id=5)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_claim_missing_and_completed_run(self) -> None:
        server, clock = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

        # Validation precedes run lookup.
        status, body = claim(server, "ghost", {"lease_seconds": 10})
        self.assertEqual(status, 400)
        status, body = claim(server, "ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "run_not_found")

        _, body = claim(server, "r1")
        submit(server, "r1", "c1[0]", claim_id=body["claims"][0]["claim_id"])
        status, completed = server.json("POST", "/v1/runs/r1/complete")
        self.assertEqual(status, 200)

        status, body = claim(server, "r1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "run_completed")

        # Completing a run while leases are outstanding still requires no
        # pending instances; once completed, claims are refused everywhere.
        server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["c1"]})
        claim(server, "r2")
        status, body = server.json("POST", "/v1/runs/r2/complete")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "run_incomplete")

    def test_retry_runs_are_claimable(self) -> None:
        server, clock = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2"]})
        submit(server, "r1", "c2[0]", outcome="failed")
        submit(server, "r1", "c2[1]", outcome="error")
        server.json("POST", "/v1/runs/r1/complete")

        status, retry = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
        self.assertEqual(status, 201)

        status, body = claim(server, "r2", {"worker_id": "w1", "max_items": 5, "lease_seconds": 30})
        self.assertEqual(status, 200)
        self.assertEqual([c["instance_id"] for c in body["claims"]], ["c2[0]", "c2[1]"])
        for item in body["claims"]:
            self.assertEqual(item["steps"], [{"action": "run"}])
            self.assertIsInstance(item["expires_at"], int)

    def test_concurrent_claims_never_double_lease(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            # 12 instances across parameterized cases.
            server.json(
                "POST",
                "/v1/cases",
                case_payload("c9", parameterization={"rows": [{"i": n} for n in range(10)]}),
            )
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c9", "c1"]})

            total = 11
            worker_count = 11
            barrier = threading.Barrier(worker_count + 1)
            results: list[dict] = []
            errors: list[object] = []
            result_lock = threading.Lock()

            def worker(index: int) -> None:
                try:
                    barrier.wait()
                    status, body = server.json(
                        "POST",
                        "/v1/runs/r1/claims",
                        {"worker_id": f"w{index}", "max_items": 1, "lease_seconds": 600},
                    )
                    assert status == 200, (status, body)
                    with result_lock:
                        results.extend(body["claims"])
                except BaseException as exc:  # pragma: no cover - failure capture
                    with result_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(results), total)
            leased_instances = [c["instance_id"] for c in results]
            self.assertEqual(len(set(leased_instances)), total)
            claim_ids = [c["claim_id"] for c in results]
            self.assertEqual(len(set(claim_ids)), total)

            # A follow-up wave finds nothing left to lease.
            status, body = server.json(
                "POST",
                "/v1/runs/r1/claims",
                {"worker_id": "late", "max_items": 100, "lease_seconds": 600},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["claims"], [])

            # Every holder can submit concurrently with its own token; all
            # submissions succeed exactly once.
            submit_barrier = threading.Barrier(total)
            submit_errors: list[object] = []

            def finish(item: dict) -> None:
                try:
                    submit_barrier.wait()
                    status, body = server.json(
                        "POST",
                        "/v1/runs/r1/results",
                        {
                            "instance_id": item["instance_id"],
                            "claim_id": item["claim_id"],
                            "outcome": "passed",
                            "duration_ms": 3,
                        },
                    )
                    assert status == 200, (status, body)
                except BaseException as exc:  # pragma: no cover
                    submit_errors.append(exc)

            threads = [threading.Thread(target=finish, args=(item,)) for item in results]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(submit_errors, [])

            status, report = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["passed"], total)

    def test_concurrent_claim_and_direct_submit(self) -> None:
        # An instance whose lease expired may be claimed again while a direct
        # (claim-less) submit races it; exactly one path must win.
        server, clock = seed_plan_server()
        self.addCleanup(lambda: server.__exit__(None, None, None))
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

        _, body = claim(server, "r1", {"worker_id": "slow", "lease_seconds": 1})
        old_expiry = body["claims"][0]["expires_at"]
        clock.now_ms = old_expiry

        claim_outcome: dict = {}
        submit_outcome: dict = {}
        barrier = threading.Barrier(2)

        def do_claim() -> None:
            barrier.wait()
            status, payload = claim(
                server, "r1", {"worker_id": "fast", "lease_seconds": 60}
            )
            claim_outcome["status"] = status
            claim_outcome["claims"] = payload["claims"]

        def do_submit() -> None:
            barrier.wait()
            status, payload = submit(server, "r1", "c1[0]")
            submit_outcome["status"] = status
            submit_outcome["body"] = payload

        threads = [threading.Thread(target=do_claim), threading.Thread(target=do_submit)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        _, report = server.json("GET", "/v1/runs/r1")
        instance = report["instances"][0]
        if claim_outcome["claims"]:
            # The new lease won: direct submit was rejected, instance pending,
            # and the granted token still submits successfully.
            self.assertEqual(len(claim_outcome["claims"]), 1)
            self.assertEqual(submit_outcome["status"], 409)
            self.assertEqual(
                submit_outcome["body"]["error"]["code"], "claim_conflict"
            )
            self.assertEqual(instance["outcome"], "pending")
            status, _ = submit(
                server,
                "r1",
                "c1[0]",
                claim_id=claim_outcome["claims"][0]["claim_id"],
            )
            self.assertEqual(status, 200)
        else:
            # Direct submit won: the claim returned an empty list and the
            # instance has exactly one result.
            self.assertEqual(claim_outcome["status"], 200)
            self.assertEqual(submit_outcome["status"], 200)
            self.assertEqual(instance["outcome"], "passed")
            self.assertEqual(instance["duration_ms"], 5)


if __name__ == "__main__":
    unittest.main()
