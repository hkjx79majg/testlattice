"""End-to-end HTTP tests for lease-based parallel scheduling (claims)."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import testlattice.service as service_module
from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases


def claim(server: ServerHarness, run_id: str, **body: object):
    payload = {"worker_id": "w", "lease_seconds": 60}
    payload.update(body)
    return server.json("POST", f"/v1/runs/{run_id}/claims", payload)


def result_payload(instance_id: str, claim_id: str | None = None, **extra: object) -> dict:
    payload: dict = {
        "instance_id": instance_id,
        "outcome": "passed",
        "duration_ms": 3,
    }
    if claim_id is not None:
        payload["claim_id"] = claim_id
    payload.update(extra)
    return payload


class ClaimTest(unittest.TestCase):
    def test_claim_returns_frozen_context_in_frozen_order(self) -> None:
        service = Service()
        with ServerHarness(service) as server:
            seed_cases(server)
            # A case with a fixture so the claim carries setup/teardown.
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
                case_payload(
                    "cf",
                    kind="api",
                    timeout_seconds=42,
                    fixture_ids=["f1"],
                ),
            )
            server.json(
                "POST", "/v1/runs", {"id": "r1", "case_ids": ["cf", "c2", "c1"]}
            )

            status, body = claim(
                server, "r1", worker_id=" worker-1 ", max_items=2, lease_seconds=30
            )
            self.assertEqual(status, 200)
            claims = body["claims"]
            self.assertEqual(
                [c["instance_id"] for c in claims], ["cf[0]", "c2[0]"]
            )

            first = claims[0]
            self.assertEqual(
                set(first),
                {
                    "claim_id",
                    "instance_id",
                    "case_id",
                    "case_name",
                    "kind",
                    "parameters",
                    "timeout_seconds",
                    "setup",
                    "steps",
                    "teardown",
                    "expires_at",
                },
            )
            self.assertIsInstance(first["claim_id"], str)
            self.assertTrue(first["claim_id"])
            self.assertEqual(first["case_id"], "cf")
            self.assertEqual(first["case_name"], "name-cf")
            self.assertEqual(first["kind"], "api")
            self.assertEqual(first["parameters"], {})
            self.assertEqual(first["timeout_seconds"], 42)
            self.assertEqual(
                first["setup"], [{"fixture_id": "f1", "steps": [{"action": "boot"}]}]
            )
            self.assertEqual(first["steps"], [{"action": "run"}])
            self.assertEqual(
                first["teardown"],
                [{"fixture_id": "f1", "steps": [{"action": "halt"}]}],
            )
            self.assertIsInstance(first["expires_at"], int)
            self.assertNotIsInstance(first["expires_at"], bool)

            second = claims[1]
            self.assertEqual(second["instance_id"], "c2[0]")
            self.assertEqual(second["parameters"], {"x": 1})
            self.assertEqual(second["setup"], [])
            self.assertEqual(second["teardown"], [])

            # Claims are unique opaque ids and share the same expiry instant.
            self.assertNotEqual(first["claim_id"], second["claim_id"])
            self.assertEqual(first["expires_at"], second["expires_at"])

    def test_default_max_items_is_one_and_leased_instances_skipped(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})

            status, body = claim(server, "r1", worker_id="a")
            self.assertEqual(status, 200)
            self.assertEqual([c["instance_id"] for c in body["claims"]], ["c2[0]"])

            status, body = claim(server, "r1", worker_id="b", max_items=10)
            self.assertEqual(status, 200)
            # The leased c2[0] is skipped; selection keeps frozen order.
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]], ["c2[1]", "c1[0]"]
            )

            # Nothing left without an active lease.
            status, body = claim(server, "r1", worker_id="c")
            self.assertEqual(status, 200)
            self.assertEqual(body, {"claims": []})

    def test_claim_skips_finished_instances(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            status, _ = server.json(
                "POST",
                "/v1/runs/r1/results",
                result_payload("c2[0]"),
            )
            self.assertEqual(status, 200)
            status, body = claim(server, "r1", max_items=10)
            self.assertEqual(status, 200)
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]], ["c2[1]", "c1[0]"]
            )

    def test_claims_work_on_retry_runs(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c1[0]", "outcome": "failed", "duration_ms": 5},
            )
            server.json("POST", "/v1/runs/r1/complete")
            status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            status, body = claim(server, "r2", max_items=5)
            self.assertEqual(status, 200)
            self.assertEqual([c["instance_id"] for c in body["claims"]], ["c1[0]"])

    def test_report_never_exposes_lease_fields(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, _ = claim(server, "r1")
            self.assertEqual(status, 200)
            status, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            instance = report["instances"][0]
            self.assertEqual(
                set(instance), {"instance_id", "case_id", "case_name", "parameters", "outcome"}
            )
            self.assertEqual(instance["outcome"], "pending")

            # A leased-but-unfinished instance still blocks completion.
            status, body = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "run_incomplete")

    def test_submit_with_matching_claim_consumes_lease(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            _, body = claim(server, "r1", max_items=2)
            ids = {c["instance_id"]: c["claim_id"] for c in body["claims"]}

            status, report = server.json(
                "POST",
                "/v1/runs/r1/results",
                result_payload("c2[0]", ids["c2[0]"], duration_ms=11),
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["passed"], 1)
            self.assertEqual(report["summary"]["duration_ms"], 11)

            # The consumed claim can never authorize a second submission.
            status, error = server.json(
                "POST",
                "/v1/runs/r1/results",
                result_payload("c2[1]", ids["c2[0]"]),
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "claim_not_active")

            status, error = server.json(
                "POST",
                "/v1/runs/r1/results",
                result_payload("c2[0]", ids["c2[0]"], outcome="failed"),
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "claim_not_active")

            # The other instance remains leased and unavailable to others.
            status, body = claim(server, "r1", max_items=10)
            self.assertEqual([c["instance_id"] for c in body["claims"]], ["c1[0]"])

    def test_submit_conflicts_while_lease_active(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            _, first = claim(server, "r1", worker_id="a")
            _, second = claim(server, "r1", worker_id="b")
            leased = first["claims"][0]
            other = second["claims"][0]

            # No claim id on a leased instance.
            status, error = server.json(
                "POST", "/v1/runs/r1/results", result_payload(leased["instance_id"])
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "claim_conflict")

            # Another instance's still-active claim id.
            status, error = server.json(
                "POST",
                "/v1/runs/r1/results",
                result_payload(leased["instance_id"], other["claim_id"]),
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "claim_conflict")

            # Unknown / foreign garbage id is not active rather than a conflict.
            status, error = server.json(
                "POST",
                "/v1/runs/r1/results",
                result_payload(leased["instance_id"], "no-such-claim"),
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "claim_not_active")

            # Nothing was written: instance is still pending and still leased.
            status, report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(report["summary"]["pending"], 3)
            status, body = claim(server, "r1", max_items=10)
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]], ["c1[0]"]
            )

    def test_failed_claim_submit_writes_no_coverage(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            _, body = claim(server, "r1")
            claim_id = body["claims"][0]["claim_id"]
            coverage = {"files": {"a.py": {"executable_lines": [1], "covered_lines": [1]}}}
            status, error = server.json(
                "POST",
                "/v1/runs/r1/results",
                result_payload(
                    "c1[0]",
                    "wrong-id",
                    coverage=coverage,
                ),
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "claim_not_active")

            # Settle the run properly; coverage must still be empty.
            server.json(
                "POST", "/v1/runs/r1/results", result_payload("c1[0]", claim_id)
            )
            server.json("POST", "/v1/runs/r1/complete")
            status, coverage_report = server.json("GET", "/v1/runs/r1/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(coverage_report["files"], [])

    def test_direct_submission_still_works_without_lease(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            status, report = server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 2},
            )
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["passed"], 1)

    def test_claim_id_on_unleased_instance_is_not_active(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            _, body = claim(server, "r1")
            claim_id = body["claims"][0]["claim_id"]
            # c1[0] holds no lease; presenting any id fails as not active.
            status, error = server.json(
                "POST", "/v1/runs/r1/results", result_payload("c1[0]", claim_id)
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "claim_not_active")

    def test_lease_expiry_makes_instance_claimable_again(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            service = Service()
            with ServerHarness(service) as server:
                seed_cases(server)
                server.json(
                    "POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]}
                )
                _, first = claim(server, "r1", max_items=2, lease_seconds=10)
                old = first["claims"][0]
                self.assertEqual(old["expires_at"], clock["now"] + 10_000)

                # One millisecond before expiry: still leased.
                clock["now"] += 9_999
                status, body = claim(server, "r1", max_items=10)
                self.assertEqual(
                    [c["instance_id"] for c in body["claims"]], ["c1[0]"]
                )

                # Exactly at expires_at the lease is gone.
                clock["now"] += 1
                status, error = server.json(
                    "POST",
                    "/v1/runs/r1/results",
                    result_payload("c2[0]", old["claim_id"]),
                )
                self.assertEqual(status, 409)
                self.assertEqual(error["error"]["code"], "claim_not_active")

                status, body = claim(server, "r1", max_items=10)
                self.assertEqual(status, 200)
                reclaimed = {c["instance_id"]: c for c in body["claims"]}
                self.assertIn("c2[0]", reclaimed)
                self.assertIn("c2[1]", reclaimed)
                self.assertNotEqual(
                    reclaimed["c2[0]"]["claim_id"], old["claim_id"]
                )
                # c1[0] was leased by the intermediate claim and is skipped.
                self.assertNotIn("c1[0]", reclaimed)

                # Expired instance also accepts a lease-less direct submit.
                clock["now"] += 60_000
                status, report = server.json(
                    "POST",
                    "/v1/runs/r1/results",
                    {"instance_id": "c2[0]", "outcome": "passed", "duration_ms": 1},
                )
                self.assertEqual(status, 200)
                self.assertEqual(report["summary"]["passed"], 1)

    def test_concurrent_claims_never_double_lease(self) -> None:
        service = Service()
        with ServerHarness(service) as server:
            seed_cases(server)
            cases = [f"p{i}" for i in range(9)]
            for cid in cases:
                server.json(
                    "POST",
                    "/v1/cases",
                    case_payload(
                        cid,
                        parameterization={"rows": [{"x": n} for n in range(3)]},
                    ),
                )
            case_ids = ["c2", "c1"] + cases
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": case_ids})

            # 30 instances; hammer with 40 concurrent claim loops.
            claimed: list[dict] = []
            claimed_lock = threading.Lock()
            barrier = threading.Barrier(40)

            def worker() -> None:
                conn = server
                barrier.wait()
                while True:
                    status, body = claim(conn, "r1", worker_id="w", max_items=1)
                    assert status == 200
                    if not body["claims"]:
                        return
                    with claimed_lock:
                        claimed.append(body["claims"][0])

            threads = [threading.Thread(target=worker) for _ in range(40)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            instance_ids = [c["instance_id"] for c in claimed]
            self.assertEqual(len(instance_ids), 30)
            self.assertEqual(len(set(instance_ids)), 30)

            # Every leased instance accepts its own claim, concurrently.
            submit_errors: list[tuple[str, int, str]] = []
            errors_lock = threading.Lock()

            def submit(claim_item: dict) -> None:
                status, body = server.json(
                    "POST",
                    "/v1/runs/r1/results",
                    result_payload(claim_item["instance_id"], claim_item["claim_id"]),
                )
                if status != 200:
                    with errors_lock:
                        submit_errors.append((claim_item["instance_id"], status, str(body)))

            submit_threads = [
                threading.Thread(target=submit, args=(item,)) for item in claimed
            ]
            for thread in submit_threads:
                thread.start()
            for thread in submit_threads:
                thread.join()
            self.assertEqual(submit_errors, [])

            status, report = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 200)
            self.assertEqual(report["summary"]["passed"], 30)

    def test_claim_resource_errors(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

            status, error = claim(server, "ghost")
            self.assertEqual(status, 404)
            self.assertEqual(error["error"]["code"], "run_not_found")

            server.json(
                "POST",
                "/v1/runs/r1/results",
                {"instance_id": "c1[0]", "outcome": "passed", "duration_ms": 1},
            )
            server.json("POST", "/v1/runs/r1/complete")
            status, error = claim(server, "r1")
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "run_completed")

    def test_claim_validation(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

            bad_payloads = [
                ["not", "object"],
                "nope",
                123,
                None,
                {},
                {"max_items": 1, "lease_seconds": 60},
                {"worker_id": "   ", "lease_seconds": 60},
                {"worker_id": 7, "lease_seconds": 60},
                {"worker_id": "w", "lease_seconds": 60, "extra": 1},
                {"worker_id": "w"},
                {"worker_id": "w", "lease_seconds": None},
                {"worker_id": "w", "lease_seconds": 0},
                {"worker_id": "w", "lease_seconds": 3601},
                {"worker_id": "w", "lease_seconds": 1.5},
                {"worker_id": "w", "lease_seconds": True},
                {"worker_id": "w", "lease_seconds": "10"},
                {"worker_id": "w", "lease_seconds": 60, "max_items": None},
                {"worker_id": "w", "lease_seconds": 60, "max_items": 0},
                {"worker_id": "w", "lease_seconds": 60, "max_items": 101},
                {"worker_id": "w", "lease_seconds": 60, "max_items": 1.5},
                {"worker_id": "w", "lease_seconds": 60, "max_items": True},
                {"worker_id": "w", "lease_seconds": 60, "max_items": "3"},
            ]
            for payload in bad_payloads:
                status, body = server.json("POST", "/v1/runs/r1/claims", payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(body["error"]["code"], "validation_error", payload)

            status, body = server.json(
                "POST", "/v1/runs/r1/claims", raw_body=b"{broken"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

            # Failed validation left no leases behind.
            status, body = claim(server, "r1", max_items=10)
            self.assertEqual(status, 200)
            self.assertEqual([c["instance_id"] for c in body["claims"]], ["c1[0]"])

    def test_claim_id_field_validation_on_results(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            _, body = claim(server, "r1")
            claim_id = body["claims"][0]["claim_id"]

            for bad in ("", 123, ["x"], {"x": 1}):
                status, error = server.json(
                    "POST",
                    "/v1/runs/r1/results",
                    {
                        "instance_id": "c1[0]",
                        "outcome": "passed",
                        "duration_ms": 1,
                        "claim_id": bad,
                    },
                )
                self.assertEqual(status, 400, bad)
                self.assertEqual(error["error"]["code"], "validation_error", bad)

            # Whitespace-only is a syntactically valid but unknown token, so
            # it is treated like any inactive claim identifier.
            status, error = server.json(
                "POST",
                "/v1/runs/r1/results",
                {
                    "instance_id": "c1[0]",
                    "outcome": "passed",
                    "duration_ms": 1,
                    "claim_id": "   ",
                },
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "claim_not_active")

            # Lease survived the rejected submissions and still redeems once.
            status, _ = server.json(
                "POST",
                "/v1/runs/r1/results",
                result_payload("c1[0]", claim_id),
            )
            self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
