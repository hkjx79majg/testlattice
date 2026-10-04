"""End-to-end HTTP tests for in-process named resource pools."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import testlattice.service as service_module
from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases


def pool_payload(pool_id: str = "gpu", **overrides: object) -> dict:
    payload: dict = {"id": pool_id, "name": f"name-{pool_id}", "capacity": 2}
    payload.update(overrides)
    return payload


def create_pool(server: ServerHarness, pool_id: str = "gpu", **overrides: object):
    return server.json("POST", "/v1/resource-pools", pool_payload(pool_id, **overrides))


def claim(server: ServerHarness, run_id: str, **body: object):
    payload = {"worker_id": "w", "lease_seconds": 60}
    payload.update(body)
    return server.json("POST", f"/v1/runs/{run_id}/claims", payload)


def submit(server: ServerHarness, run_id: str, instance_id: str, **extra: object):
    payload: dict = {
        "instance_id": instance_id,
        "outcome": "passed",
        "duration_ms": 1,
    }
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


class ResourcePoolCrudTest(unittest.TestCase):
    def test_create_get_list_delete(self) -> None:
        with ServerHarness() as server:
            status, pool = create_pool(server, "gpu", capacity=4)
            self.assertEqual(status, 201)
            self.assertEqual(
                pool,
                {
                    "id": "gpu",
                    "name": "name-gpu",
                    "capacity": 4,
                    "allocated": 0,
                    "available": 4,
                },
            )

            status, _ = create_pool(server, "cpu", capacity=1)
            self.assertEqual(status, 201)

            status, pools = server.json("GET", "/v1/resource-pools")
            self.assertEqual(status, 200)
            # Creation order is preserved.
            self.assertEqual([p["id"] for p in pools], ["gpu", "cpu"])
            self.assertEqual(pools[1]["allocated"], 0)
            self.assertEqual(pools[1]["available"], 1)

            status, fetched = server.json("GET", "/v1/resource-pools/gpu")
            self.assertEqual(status, 200)
            self.assertEqual(fetched, pool)

            status, _, _ = server.request("DELETE", "/v1/resource-pools/gpu")
            self.assertEqual(status, 204)
            status, error = server.json("GET", "/v1/resource-pools/gpu")
            self.assertEqual(status, 404)
            self.assertEqual(error["error"]["code"], "resource_pool_not_found")
            status, pools = server.json("GET", "/v1/resource-pools")
            self.assertEqual([p["id"] for p in pools], ["cpu"])

    def test_ids_are_trimmed_and_must_be_unique(self) -> None:
        with ServerHarness() as server:
            status, pool = create_pool(server, " gpu ")
            self.assertEqual(status, 201)
            self.assertEqual(pool["id"], "gpu")

            status, error = create_pool(server, "gpu")
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "resource_pool_exists")
            # The trimmed id collides too.
            status, error = create_pool(server, " gpu")
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "resource_pool_exists")

    def test_missing_pool_read_and_delete_are_404(self) -> None:
        with ServerHarness() as server:
            status, error = server.json("GET", "/v1/resource-pools/ghost")
            self.assertEqual(status, 404)
            self.assertEqual(error["error"]["code"], "resource_pool_not_found")
            status, error = server.json("DELETE", "/v1/resource-pools/ghost")
            self.assertEqual(status, 404)
            self.assertEqual(error["error"]["code"], "resource_pool_not_found")

    def test_validation_errors_leave_nothing_behind(self) -> None:
        with ServerHarness() as server:
            bad_bodies = [
                "not-an-object",
                [],
                {"name": "n", "capacity": 1},  # missing id
                {"id": "p", "capacity": 1},  # missing name
                {"id": "p", "name": "n"},  # missing capacity
                {"id": " ", "name": "n", "capacity": 1},
                {"id": "p", "name": "", "capacity": 1},
                {"id": "p", "name": "n", "capacity": True},
                {"id": "p", "name": "n", "capacity": 1.5},
                {"id": "p", "name": "n", "capacity": "2"},
                {"id": "p", "name": "n", "capacity": 0},
                {"id": "p", "name": "n", "capacity": -3},
                {"id": "p", "name": "n", "capacity": 10001},
                {"id": "p", "name": "n", "capacity": 1, "extra": 1},
            ]
            for body in bad_bodies:
                status, error = server.json("POST", "/v1/resource-pools", body)
                self.assertEqual(status, 400, body)
                self.assertEqual(error["error"]["code"], "validation_error", body)

            status, error = server.json(
                "POST", "/v1/resource-pools", raw_body=b"{not json"
            )
            self.assertEqual(status, 400)
            self.assertEqual(error["error"]["code"], "invalid_json")

            status, pools = server.json("GET", "/v1/resource-pools")
            self.assertEqual(status, 200)
            self.assertEqual(pools, [])

            # Boundary capacities are accepted.
            status, pool = create_pool(server, "min", capacity=1)
            self.assertEqual(status, 201)
            status, pool = create_pool(server, "max", capacity=10000)
            self.assertEqual(status, 201)
            self.assertEqual(pool["available"], 10000)


class ResourceRequirementCaseTest(unittest.TestCase):
    def test_case_round_trips_requirements(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            create_pool(server, "gpu", capacity=4)
            status, case = server.json(
                "POST",
                "/v1/cases",
                case_payload("cr", resource_requirements={"gpu": 2}),
            )
            self.assertEqual(status, 201)
            self.assertEqual(case["resource_requirements"], {"gpu": 2})

            status, fetched = server.json("GET", "/v1/cases/cr")
            self.assertEqual(status, 200)
            self.assertEqual(fetched["resource_requirements"], {"gpu": 2})

            status, cases = server.json("GET", "/v1/cases")
            self.assertEqual(status, 200)
            by_id = {c["id"]: c for c in cases}
            self.assertEqual(by_id["cr"]["resource_requirements"], {"gpu": 2})
            # Cases without the field are not given one.
            self.assertNotIn("resource_requirements", by_id["c1"])

            status, plan = server.json("GET", "/v1/cases/cr/execution-plan")
            self.assertEqual(status, 200)
            self.assertEqual(plan["count"], 1)
            self.assertEqual(
                plan["instances"][0]["resource_requirements"], {"gpu": 2}
            )

            # The execution plan of a case without requirements is unchanged.
            status, plan = server.json("GET", "/v1/cases/c1/execution-plan")
            self.assertEqual(status, 200)
            self.assertNotIn("resource_requirements", plan["instances"][0])

    def test_unknown_pool_reference_is_404(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            status, error = server.json(
                "POST",
                "/v1/cases",
                case_payload("cr", resource_requirements={"ghost": 1}),
            )
            self.assertEqual(status, 404)
            self.assertEqual(error["error"]["code"], "resource_pool_not_found")
            status, error = server.json("GET", "/v1/cases/cr")
            self.assertEqual(status, 404)

    def test_requirement_above_capacity_is_rejected(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            create_pool(server, "gpu", capacity=2)
            status, error = server.json(
                "POST",
                "/v1/cases",
                case_payload("cr", resource_requirements={"gpu": 3}),
            )
            self.assertEqual(status, 400)
            self.assertEqual(error["error"]["code"], "validation_error")
            status, error = server.json("GET", "/v1/cases/cr")
            self.assertEqual(status, 404)

    def test_malformed_requirements_are_validation_errors(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            create_pool(server, "gpu", capacity=2)
            bad_values = [
                "gpu",
                ["gpu"],
                {"gpu": 0},
                {"gpu": -1},
                {"gpu": 1.5},
                {"gpu": True},
                {"gpu": "1"},
                {"gpu": None},
                {"": 1},
            ]
            for value in bad_values:
                status, error = server.json(
                    "POST",
                    "/v1/cases",
                    case_payload("cr", resource_requirements=value),
                )
                self.assertEqual(status, 400, value)
                self.assertEqual(error["error"]["code"], "validation_error", value)
            status, error = server.json("GET", "/v1/cases/cr")
            self.assertEqual(status, 404)


class ResourceClaimTest(unittest.TestCase):
    def _seed_pool_case(
        self, server: ServerHarness, requirements: dict, case_id: str = "cr"
    ) -> None:
        seed_cases(server)
        create_pool(server, "gpu", capacity=2)
        server.json(
            "POST",
            "/v1/cases",
            case_payload(
                case_id,
                parameterization={"axes": {"x": [1, 2, 3]}},
                resource_requirements=requirements,
            ),
        )

    def test_claim_allocates_and_reports_requirements(self) -> None:
        with ServerHarness() as server:
            self._seed_pool_case(server, {"gpu": 1})
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cr"]})

            status, body = claim(server, "r1", max_items=2)
            self.assertEqual(status, 200)
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]], ["cr[0]", "cr[1]"]
            )
            self.assertEqual(body["claims"][0]["resource_requirements"], {"gpu": 1})

            status, pool = server.json("GET", "/v1/resource-pools/gpu")
            self.assertEqual(pool["allocated"], 2)
            self.assertEqual(pool["available"], 0)

            # Capacity exhausted: the third instance is skipped for now.
            status, body = claim(server, "r1", max_items=1)
            self.assertEqual(status, 200)
            self.assertEqual(body["claims"], [])

    def test_unsatisfiable_instance_does_not_block_later_ones(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            create_pool(server, "gpu", capacity=1)
            server.json(
                "POST",
                "/v1/cases",
                case_payload(
                    "heavy",
                    parameterization={"axes": {"x": [1, 2]}},
                    resource_requirements={"gpu": 1},
                ),
            )
            server.json(
                "POST", "/v1/runs", {"id": "r1", "case_ids": ["heavy", "c1"]}
            )

            # First claim takes the only gpu unit for heavy[0].
            status, body = claim(server, "r1", max_items=1)
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]], ["heavy[0]"]
            )
            # heavy[1] cannot be satisfied, but c1[0] needs nothing and is
            # still leased; max_items is not exceeded.
            status, body = claim(server, "r1", max_items=5)
            self.assertEqual(status, 200)
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]], ["c1[0]"]
            )
            self.assertNotIn("resource_requirements", body["claims"][0])

    def test_submit_releases_allocation(self) -> None:
        with ServerHarness() as server:
            self._seed_pool_case(server, {"gpu": 2})
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cr"]})

            _, body = claim(server, "r1", max_items=1)
            first = body["claims"][0]
            self.assertEqual(first["resource_requirements"], {"gpu": 2})
            _, pool = server.json("GET", "/v1/resource-pools/gpu")
            self.assertEqual(pool["allocated"], 2)

            status, _ = submit(
                server, "r1", "cr[0]", claim_id=first["claim_id"]
            )
            self.assertEqual(status, 200)
            _, pool = server.json("GET", "/v1/resource-pools/gpu")
            self.assertEqual(pool["allocated"], 0)
            self.assertEqual(pool["available"], 2)

            # The freed capacity is immediately claimable again.
            status, body = claim(server, "r1", max_items=1)
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]], ["cr[1]"]
            )

    def test_timeout_releases_allocation(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            service = Service()
            with ServerHarness(service) as server:
                seed_cases(server)
                create_pool(server, "gpu", capacity=1)
                server.json(
                    "POST",
                    "/v1/cases",
                    case_payload(
                        "cr", timeout_seconds=5, resource_requirements={"gpu": 1}
                    ),
                )
                server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cr"]})
                _, body = claim(server, "r1", lease_seconds=600)
                self.assertEqual(len(body["claims"]), 1)
                _, pool = server.json("GET", "/v1/resource-pools/gpu")
                self.assertEqual(pool["allocated"], 1)

                clock["now"] += 5_000
                status, result = server.json("POST", "/v1/runs/r1/timeouts", {})
                self.assertEqual(status, 200)
                self.assertEqual(result["timed_out"], ["cr[0]"])
                _, pool = server.json("GET", "/v1/resource-pools/gpu")
                self.assertEqual(pool["allocated"], 0)

    def test_lease_expiry_releases_allocation(self) -> None:
        clock = {"now": 1_000_000_000_000}
        with mock.patch.object(service_module, "_now_ms", lambda: clock["now"]):
            service = Service()
            with ServerHarness(service) as server:
                self._seed_pool_case(server, {"gpu": 1})
                server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cr"]})
                _, body = claim(server, "r1", max_items=2, lease_seconds=10)
                self.assertEqual(len(body["claims"]), 2)
                _, pool = server.json("GET", "/v1/resource-pools/gpu")
                self.assertEqual(pool["allocated"], 2)

                # Past expiry the pool read reflects the release immediately,
                # even though no claim touched the run in between.
                clock["now"] += 10_000
                _, pool = server.json("GET", "/v1/resource-pools/gpu")
                self.assertEqual(pool["allocated"], 0)
                self.assertEqual(pool["available"], 2)

                # All three instances are pending and unleased again, but
                # capacity 2 still bounds how many leases can go out.
                status, body = claim(server, "r1", max_items=3)
                self.assertEqual(status, 200)
                self.assertEqual(len(body["claims"]), 2)
                _, pool = server.json("GET", "/v1/resource-pools/gpu")
                self.assertEqual(pool["allocated"], 2)

    def test_allocation_is_shared_across_open_runs(self) -> None:
        with ServerHarness() as server:
            self._seed_pool_case(server, {"gpu": 1})
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cr"]})
            server.json("POST", "/v1/runs", {"id": "r2", "case_ids": ["cr"]})

            _, body = claim(server, "r1", max_items=2)
            self.assertEqual(len(body["claims"]), 2)
            # Both units are held by r1; r2 cannot allocate until release.
            status, body = claim(server, "r2", max_items=3)
            self.assertEqual(status, 200)
            self.assertEqual(body["claims"], [])

    def test_concurrent_claims_never_exceed_capacity(self) -> None:
        service = Service()
        with ServerHarness(service) as server:
            seed_cases(server)
            create_pool(server, "gpu", capacity=3)
            server.json(
                "POST",
                "/v1/cases",
                case_payload(
                    "cr",
                    parameterization={"rows": [{"x": n} for n in range(12)]},
                    resource_requirements={"gpu": 1},
                ),
            )
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cr"]})

            claimed: list[dict] = []
            claimed_lock = threading.Lock()
            barrier = threading.Barrier(12)

            def worker() -> None:
                barrier.wait()
                while True:
                    status, body = claim(server, "r1", max_items=1)
                    assert status == 200
                    if not body["claims"]:
                        return
                    with claimed_lock:
                        claimed.append(body["claims"][0])

            threads = [threading.Thread(target=worker) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            # Workers never submit, so leases are never released: exactly
            # capacity-many distinct instances are ever leased, never more.
            self.assertEqual(len(claimed), 3)
            self.assertEqual(
                len({c["instance_id"] for c in claimed}), 3
            )
            _, pool = server.json("GET", "/v1/resource-pools/gpu")
            self.assertEqual(pool["allocated"], 3)
            self.assertEqual(pool["available"], 0)


class ResourcePoolDeletionTest(unittest.TestCase):
    def test_pool_referenced_by_catalog_case_cannot_be_deleted(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            create_pool(server, "gpu", capacity=2)
            server.json(
                "POST",
                "/v1/cases",
                case_payload("cr", resource_requirements={"gpu": 1}),
            )
            status, error = server.json("DELETE", "/v1/resource-pools/gpu")
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "resource_pool_in_use")

            server.request("DELETE", "/v1/cases/cr")
            status, _, _ = server.request("DELETE", "/v1/resource-pools/gpu")
            self.assertEqual(status, 204)

    def test_open_run_with_frozen_requirements_blocks_deletion(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            create_pool(server, "gpu", capacity=2)
            server.json(
                "POST",
                "/v1/cases",
                case_payload("cr", resource_requirements={"gpu": 1}),
            )
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cr"]})
            # The catalog case is gone, but the frozen run still needs it.
            server.request("DELETE", "/v1/cases/cr")
            status, error = server.json("DELETE", "/v1/resource-pools/gpu")
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "resource_pool_in_use")

            # Frozen runs still claim against the frozen requirements.
            status, body = claim(server, "r1")
            self.assertEqual(status, 200)
            self.assertEqual(
                body["claims"][0]["resource_requirements"], {"gpu": 1}
            )

            # Finishing the run unblocks deletion.
            submit(server, "r1", "cr[0]", claim_id=body["claims"][0]["claim_id"])
            status, _ = server.json("POST", "/v1/runs/r1/complete")
            self.assertEqual(status, 200)
            status, _, _ = server.request("DELETE", "/v1/resource-pools/gpu")
            self.assertEqual(status, 204)

    def test_case_deletion_keeps_frozen_run_claiming(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            create_pool(server, "gpu", capacity=1)
            server.json(
                "POST",
                "/v1/cases",
                case_payload(
                    "cr",
                    parameterization={"axes": {"x": [1, 2]}},
                    resource_requirements={"gpu": 1},
                ),
            )
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cr"]})
            server.request("DELETE", "/v1/cases/cr")

            _, body = claim(server, "r1", max_items=2)
            # Capacity 1: only the first instance is leased.
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]], ["cr[0]"]
            )
            submit(server, "r1", "cr[0]", claim_id=body["claims"][0]["claim_id"])
            _, body = claim(server, "r1", max_items=2)
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]], ["cr[1]"]
            )


class ResourceCompatibilityTest(unittest.TestCase):
    def test_existing_behavior_is_unchanged_without_requirements(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            status, body = claim(server, "r1", max_items=3)
            self.assertEqual(status, 200)
            self.assertEqual(
                [c["instance_id"] for c in body["claims"]],
                ["c2[0]", "c2[1]", "c1[0]"],
            )
            for item in body["claims"]:
                self.assertNotIn("resource_requirements", item)

            status, case = server.json("GET", "/v1/cases/c1")
            self.assertNotIn("resource_requirements", case)
            status, report = server.json("GET", "/v1/runs/r1")
            self.assertNotIn("resource_requirements", report["instances"][0])


if __name__ == "__main__":
    unittest.main()
