"""End-to-end HTTP tests for named in-process resource pools."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import testlattice.service as service_module
from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases


def make_server(test: unittest.TestCase, service: Service | None = None) -> ServerHarness:
    harness = ServerHarness(service or Service())
    harness.__enter__()
    test.addCleanup(harness.__exit__, None, None, None)
    return harness


def create_pool(server: ServerHarness, pool_id: str = "p1", **overrides: object):
    payload: dict = {"id": pool_id, "name": f"pool-{pool_id}", "capacity": 2}
    payload.update(overrides)
    return server.json("POST", "/v1/resource-pools", payload)


def claim(server: ServerHarness, run_id: str, **body: object):
    payload = {"worker_id": "w", "lease_seconds": 60}
    payload.update(body)
    return server.json("POST", f"/v1/runs/{run_id}/claims", payload)


def delete(server: ServerHarness, path: str) -> int:
    status, _, _ = server.request("DELETE", path)
    return status


class ResourcePoolCrudTest(unittest.TestCase):
    def test_create_get_list_delete(self) -> None:
        server = make_server(self)
        status, pool = create_pool(server, "p1", capacity=3)
        self.assertEqual(status, 201)
        self.assertEqual(
            pool,
            {
                "id": "p1",
                "name": "pool-p1",
                "capacity": 3,
                "allocated": 0,
                "available": 3,
            },
        )

        status, pool2 = create_pool(server, "p2", name=" second ", capacity=1)
        self.assertEqual(status, 201)
        self.assertEqual(pool2["name"], "second")

        status, pools = server.json("GET", "/v1/resource-pools")
        self.assertEqual(status, 200)
        self.assertEqual([p["id"] for p in pools], ["p1", "p2"])

        status, fetched = server.json("GET", "/v1/resource-pools/p1")
        self.assertEqual(status, 200)
        self.assertEqual(fetched, pool)

        self.assertEqual(delete(server, "/v1/resource-pools/p1"), 204)
        status, body = server.json("GET", "/v1/resource-pools/p1")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "resource_pool_not_found")
        # The id can be reused after deletion.
        status, _ = create_pool(server, "p1")
        self.assertEqual(status, 201)

    def test_create_trims_id_and_name(self) -> None:
        server = make_server(self)
        status, pool = create_pool(server, "  p9  ", name="  named  ")
        self.assertEqual(status, 201)
        self.assertEqual(pool["id"], "p9")
        self.assertEqual(pool["name"], "named")

    def test_missing_pool_404s(self) -> None:
        server = make_server(self)
        status, body = server.json("GET", "/v1/resource-pools/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "resource_pool_not_found")
        status, _, data = server.request("DELETE", "/v1/resource-pools/nope")
        self.assertEqual(status, 404)
        self.assertIn("resource_pool_not_found", data.decode())

    def test_duplicate_id_conflict(self) -> None:
        server = make_server(self)
        create_pool(server, "p1")
        status, body = create_pool(server, "p1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "resource_pool_exists")

    def test_validation_errors_leave_nothing_behind(self) -> None:
        server = make_server(self)
        bad_payloads = [
            "not-a-dict",
            {"name": "x", "capacity": 1},
            {"id": "x", "capacity": 1},
            {"id": "x", "name": "y"},
            {"id": "  ", "name": "y", "capacity": 1},
            {"id": "x", "name": "  ", "capacity": 1},
            {"id": "x", "name": "y", "capacity": True},
            {"id": "x", "name": "y", "capacity": 1.5},
            {"id": "x", "name": "y", "capacity": "2"},
            {"id": "x", "name": "y", "capacity": 0},
            {"id": "x", "name": "y", "capacity": 10001},
            {"id": "x", "name": "y", "capacity": 1, "extra": 1},
        ]
        for payload in bad_payloads:
            status, body = server.json("POST", "/v1/resource-pools", payload)
            self.assertEqual(status, 400, payload)
            self.assertEqual(body["error"]["code"], "validation_error")
        status, body = server.json(
            "POST", "/v1/resource-pools", raw_body=b"{not json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")
        status, pools = server.json("GET", "/v1/resource-pools")
        self.assertEqual(pools, [])

    def test_capacity_boundaries(self) -> None:
        server = make_server(self)
        status, _ = create_pool(server, "lo", capacity=1)
        self.assertEqual(status, 201)
        status, _ = create_pool(server, "hi", capacity=10000)
        self.assertEqual(status, 201)


class CaseRequirementsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = make_server(self)
        seed_cases(self.server)
        create_pool(self.server, "p1", capacity=2)

    def test_requirements_round_trip_and_execution_plan(self) -> None:
        server = self.server
        status, case = server.json(
            "POST",
            "/v1/cases",
            case_payload("cr", resource_requirements={"p1": 2}),
        )
        self.assertEqual(status, 201)
        self.assertEqual(case["resource_requirements"], {"p1": 2})

        status, fetched = server.json("GET", "/v1/cases/cr")
        self.assertEqual(fetched["resource_requirements"], {"p1": 2})

        status, cases = server.json("GET", "/v1/cases")
        by_id = {c["id"]: c for c in cases}
        self.assertEqual(by_id["cr"]["resource_requirements"], {"p1": 2})
        # Cases without the field are not given a default.
        self.assertNotIn("resource_requirements", by_id["c1"])

        status, plan = server.json("GET", "/v1/cases/cr/execution-plan")
        self.assertEqual(status, 200)
        self.assertEqual(plan["instances"][0]["resource_requirements"], {"p1": 2})

        status, plan = server.json("GET", "/v1/cases/c1/execution-plan")
        self.assertNotIn("resource_requirements", plan["instances"][0])

    def test_unknown_pool_rejected(self) -> None:
        status, body = self.server.json(
            "POST",
            "/v1/cases",
            case_payload("cx", resource_requirements={"ghost": 1}),
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "resource_pool_not_found")
        status, _ = self.server.json("GET", "/v1/cases/cx")
        self.assertEqual(status, 404)

    def test_requirement_above_capacity_rejected(self) -> None:
        status, body = self.server.json(
            "POST",
            "/v1/cases",
            case_payload("cx", resource_requirements={"p1": 3}),
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        status, _ = self.server.json("GET", "/v1/cases/cx")
        self.assertEqual(status, 404)

    def test_malformed_requirements_rejected(self) -> None:
        bad_values = [
            "nope",
            ["p1"],
            {"p1": 0},
            {"p1": -1},
            {"p1": True},
            {"p1": 1.5},
            {"p1": "1"},
            {"p1": None},
        ]
        for value in bad_values:
            status, body = self.server.json(
                "POST",
                "/v1/cases",
                case_payload("cx", resource_requirements=value),
            )
            self.assertEqual(status, 400, value)
            self.assertEqual(body["error"]["code"], "validation_error")
        status, _ = self.server.json("GET", "/v1/cases/cx")
        self.assertEqual(status, 404)


class ClaimAllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = make_server(self)
        seed_cases(self.server)
        create_pool(self.server, "p1", capacity=2)
        create_pool(self.server, "p2", capacity=5)

    def create_case(self, case_id: str, requirements: dict | None = None, **kw):
        payload = case_payload(case_id, **kw)
        if requirements is not None:
            payload["resource_requirements"] = requirements
        status, _ = self.server.json("POST", "/v1/cases", payload)
        self.assertEqual(status, 201)

    def pool(self, pool_id: str) -> dict:
        status, pool = self.server.json("GET", f"/v1/resource-pools/{pool_id}")
        self.assertEqual(status, 200)
        return pool

    def test_claim_allocates_and_submit_releases(self) -> None:
        server = self.server
        self.create_case("heavy", {"p1": 2})
        self.create_case("light", {"p1": 1})
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["heavy", "light"]})

        # heavy[0] takes the whole pool; light[0] no longer fits.
        status, body = claim(server, "r1", max_items=10)
        self.assertEqual(status, 200)
        self.assertEqual([c["instance_id"] for c in body["claims"]], ["heavy[0]"])
        self.assertEqual(body["claims"][0]["resource_requirements"], {"p1": 2})
        self.assertEqual(
            {k: self.pool("p1")[k] for k in ("allocated", "available")},
            {"allocated": 2, "available": 0},
        )

        # Submitting the result releases the allocation immediately.
        claim_id = body["claims"][0]["claim_id"]
        status, _ = server.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": "heavy[0]",
                "outcome": "passed",
                "duration_ms": 5,
                "claim_id": claim_id,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.pool("p1")["allocated"], 0)

        status, body = claim(server, "r1", max_items=10)
        self.assertEqual([c["instance_id"] for c in body["claims"]], ["light[0]"])
        self.assertEqual(self.pool("p1")["allocated"], 1)

    def test_unsatisfiable_instance_does_not_block_later_ones(self) -> None:
        server = self.server
        self.create_case("a", {"p1": 1})
        self.create_case("b", {"p1": 2})
        self.create_case("c", {"p2": 1})
        server.json(
            "POST", "/v1/runs", {"id": "r1", "case_ids": ["a", "b", "c"]}
        )
        # p1 has capacity 2: a[0] (1) fits, b[0] (2 more) does not, c[0] uses p2.
        status, body = claim(server, "r1", max_items=10)
        self.assertEqual(status, 200)
        self.assertEqual(
            [c["instance_id"] for c in body["claims"]], ["a[0]", "c[0]"]
        )
        self.assertEqual(self.pool("p1")["allocated"], 1)
        self.assertEqual(self.pool("p2")["allocated"], 1)

    def test_max_items_still_caps_response(self) -> None:
        server = self.server
        self.create_case("many", {"p2": 1}, parameterization={"axes": {"x": [1, 2, 3]}})
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["many"]})
        status, body = claim(server, "r1", max_items=2)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["claims"]), 2)
        self.assertEqual(self.pool("p2")["allocated"], 2)

    def test_claim_without_requirements_has_no_resource_field(self) -> None:
        server = self.server
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
        status, body = claim(server, "r1")
        self.assertEqual(status, 200)
        self.assertNotIn("resource_requirements", body["claims"][0])

    def test_allocation_spans_open_runs(self) -> None:
        server = self.server
        self.create_case("heavy", {"p1": 2})
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["heavy"]})
        server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["heavy"]})
        status, body = claim(server, "r1")
        self.assertEqual(len(body["claims"]), 1)
        # The other open run sees the same pool as fully allocated.
        status, body = claim(server, "r2")
        self.assertEqual(status, 200)
        self.assertEqual(body["claims"], [])

    def test_lease_expiry_releases_allocation(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(
            service_module, "_now_ms", lambda: clock["now"]
        ):
            server = self.server
            self.create_case("heavy", {"p1": 2})
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["heavy"]})
            status, body = claim(server, "r1", lease_seconds=10)
            self.assertEqual(len(body["claims"]), 1)
            self.assertEqual(self.pool("p1")["allocated"], 2)

            clock["now"] += 10_000
            # Expired lease: allocation is gone and the instance is claimable.
            self.assertEqual(self.pool("p1")["allocated"], 0)
            status, body = claim(server, "r1", lease_seconds=10)
            self.assertEqual(len(body["claims"]), 1)
            self.assertEqual(self.pool("p1")["allocated"], 2)

    def test_timeout_releases_allocation(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(
            service_module, "_now_ms", lambda: clock["now"]
        ):
            server = self.server
            self.create_case("heavy", {"p1": 2}, timeout_seconds=5)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["heavy"]})
            claim(server, "r1", lease_seconds=3600)
            self.assertEqual(self.pool("p1")["allocated"], 2)

            clock["now"] += 5_000
            status, body = server.json("POST", "/v1/runs/r1/timeouts", {})
            self.assertEqual(status, 200)
            self.assertEqual(body["timed_out"], ["heavy[0]"])
            self.assertEqual(self.pool("p1")["allocated"], 0)

    def test_frozen_run_survives_case_deletion(self) -> None:
        server = self.server
        self.create_case("heavy", {"p1": 2})
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["heavy"]})
        self.assertEqual(delete(server, "/v1/cases/heavy"), 204)
        status, body = claim(server, "r1")
        self.assertEqual(status, 200)
        self.assertEqual(body["claims"][0]["resource_requirements"], {"p1": 2})
        self.assertEqual(self.pool("p1")["allocated"], 2)

    def test_concurrent_claims_never_overallocate(self) -> None:
        server = self.server
        self.create_case(
            "many", {"p1": 1}, parameterization={"axes": {"x": list(range(8))}}
        )
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["many"]})

        results: list[list] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                _, body = claim(server, "r1", max_items=10)
                results.append(body["claims"])
            except BaseException as exc:  # pragma: no cover - diagnostic
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        claimed = [item for batch in results for item in batch]
        # p1 has capacity 2, so at most two leases can be live at once.
        self.assertLessEqual(len(claimed), 2)
        self.assertEqual(len({c["instance_id"] for c in claimed}), len(claimed))
        self.assertEqual(self.pool("p1")["allocated"], len(claimed))


class PoolDeletionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = make_server(self)
        seed_cases(self.server)
        create_pool(self.server, "p1", capacity=2)

    def create_case(self, case_id: str = "heavy") -> None:
        status, _ = self.server.json(
            "POST",
            "/v1/cases",
            case_payload(case_id, resource_requirements={"p1": 1}),
        )
        self.assertEqual(status, 201)

    def test_delete_blocked_by_catalog_case(self) -> None:
        self.create_case()
        status, _, data = self.server.request("DELETE", "/v1/resource-pools/p1")
        self.assertEqual(status, 409)
        self.assertIn("resource_pool_in_use", data.decode())
        # Deleting the referencing case unblocks the pool.
        self.assertEqual(delete(self.server, "/v1/cases/heavy"), 204)
        self.assertEqual(delete(self.server, "/v1/resource-pools/p1"), 204)

    def test_delete_blocked_by_open_run_even_after_case_delete(self) -> None:
        self.create_case()
        self.server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["heavy"]})
        self.assertEqual(delete(self.server, "/v1/cases/heavy"), 204)
        status, _, data = self.server.request("DELETE", "/v1/resource-pools/p1")
        self.assertEqual(status, 409)
        self.assertIn("resource_pool_in_use", data.decode())

    def test_completed_run_does_not_block_delete(self) -> None:
        self.create_case()
        server = self.server
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["heavy"]})
        _, body = claim(server, "r1")
        claim_id = body["claims"][0]["claim_id"]
        server.json(
            "POST",
            "/v1/runs/r1/results",
            {
                "instance_id": "heavy[0]",
                "outcome": "failed",
                "duration_ms": 1,
                "claim_id": claim_id,
            },
        )
        status, _ = server.json("POST", "/v1/runs/r1/complete")
        self.assertEqual(status, 200)
        self.assertEqual(delete(server, "/v1/cases/heavy"), 204)
        self.assertEqual(delete(server, "/v1/resource-pools/p1"), 204)


class RetryRequirementsTest(unittest.TestCase):
    def test_retry_run_keeps_frozen_requirements(self) -> None:
        server = make_server(self)
        seed_cases(server)
        create_pool(server, "p1", capacity=1)
        server.json(
            "POST",
            "/v1/cases",
            case_payload("heavy", resource_requirements={"p1": 1}),
        )
        server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["heavy"]})
        server.json(
            "POST",
            "/v1/runs/r1/results",
            {"instance_id": "heavy[0]", "outcome": "failed", "duration_ms": 1},
        )
        server.json("POST", "/v1/runs/r1/complete")
        status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
        self.assertEqual(status, 201)

        status, body = claim(server, "r2")
        self.assertEqual(status, 200)
        self.assertEqual(body["claims"][0]["resource_requirements"], {"p1": 1})
        self.assertEqual(
            server.json("GET", "/v1/resource-pools/p1")[1]["allocated"], 1
        )


if __name__ == "__main__":
    unittest.main()
