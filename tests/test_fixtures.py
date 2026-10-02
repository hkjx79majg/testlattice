"""Tests for reusable fixtures, case fixture_ids, and the execution plan."""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from testlattice.server import Handler
from testlattice.service import ApiError, Service


def fixture_payload(fixture_id: str = "f1", **overrides: object) -> dict:
    payload: dict = {
        "id": fixture_id,
        "name": "fixture name",
        "setup_steps": [{"action": "setup"}],
        "teardown_steps": [{"action": "teardown"}],
    }
    payload.update(overrides)
    return payload


def case_payload(case_id: str = "c1", suite_id: str = "s1", **overrides: object) -> dict:
    payload: dict = {
        "id": case_id,
        "name": "case name",
        "suite_id": suite_id,
        "kind": "unit",
        "steps": [{"action": "run"}],
    }
    payload.update(overrides)
    return payload


class FixtureServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.create_suite({"id": "s1", "name": "S1"})

    def create(self, payload: dict | None = None, **overrides: object) -> dict:
        return self.service.create_fixture(
            payload if payload is not None else fixture_payload(**overrides)
        )

    # -- creation ---------------------------------------------------------

    def test_create_returns_all_fields(self) -> None:
        fixture = self.create(
            setup_steps=[{"action": "boot", "timeout": 5}],
            teardown_steps=[{"action": "halt"}],
            dependencies=[],
        )
        self.assertEqual(fixture["id"], "f1")
        self.assertEqual(fixture["name"], "fixture name")
        self.assertEqual(fixture["setup_steps"], [{"action": "boot", "timeout": 5}])
        self.assertEqual(fixture["teardown_steps"], [{"action": "halt"}])
        self.assertEqual(fixture["dependencies"], [])

    def test_id_and_name_are_trimmed(self) -> None:
        fixture = self.create(id="  f1  ", name="  padded  ")
        self.assertEqual(fixture["id"], "f1")
        self.assertEqual(fixture["name"], "padded")

    def test_dependencies_omitted_when_not_provided(self) -> None:
        fixture = self.create()
        self.assertNotIn("dependencies", fixture)
        self.assertNotIn("dependencies", self.service.get_fixture("f1"))
        self.assertNotIn("dependencies", self.service.list_fixtures()[0])

    def test_setup_only_or_teardown_only_is_allowed(self) -> None:
        fixture = self.create(setup_steps=[{"action": "s"}], teardown_steps=[])
        self.assertEqual(fixture["teardown_steps"], [])
        other = self.create(fixture_payload("f2", setup_steps=[], teardown_steps=[{"action": "t"}]))
        self.assertEqual(other["setup_steps"], [])

    def test_list_returns_creation_order(self) -> None:
        self.create(fixture_payload("f2"))
        self.create(fixture_payload("f1"))
        self.create(fixture_payload("f3"))
        self.assertEqual([f["id"] for f in self.service.list_fixtures()], ["f2", "f1", "f3"])

    def test_dependencies_keep_declaration_order(self) -> None:
        self.create(fixture_payload("base-b"))
        self.create(fixture_payload("base-a"))
        fixture = self.create(dependencies=["base-b", "base-a"])
        self.assertEqual(fixture["dependencies"], ["base-b", "base-a"])

    # -- validation ---------------------------------------------------------

    def assert_validation(self, payload: object) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.service.create_fixture(payload)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual(self.service.list_fixtures(), [])

    def test_rejects_non_object_body(self) -> None:
        self.assert_validation([1, 2])

    def test_rejects_unknown_fields(self) -> None:
        self.assert_validation(fixture_payload(extra="nope"))

    def test_rejects_missing_required_fields(self) -> None:
        for field in ("id", "name", "setup_steps", "teardown_steps"):
            payload = fixture_payload()
            del payload[field]
            self.assert_validation(payload)

    def test_rejects_blank_id_and_name(self) -> None:
        self.assert_validation(fixture_payload(id="   "))
        self.assert_validation(fixture_payload(name=" \t "))
        self.assert_validation(fixture_payload(id=7))

    def test_rejects_both_step_arrays_empty(self) -> None:
        self.assert_validation(fixture_payload(setup_steps=[], teardown_steps=[]))

    def test_rejects_non_array_steps(self) -> None:
        self.assert_validation(fixture_payload(setup_steps="x"))
        self.assert_validation(fixture_payload(teardown_steps=None))

    def test_rejects_invalid_steps(self) -> None:
        self.assert_validation(fixture_payload(setup_steps=["nope"]))
        self.assert_validation(fixture_payload(teardown_steps=[{"action": ""}]))
        self.assert_validation(fixture_payload(setup_steps=[{"action": 3}]))

    def test_rejects_invalid_dependencies(self) -> None:
        self.assert_validation(fixture_payload(dependencies="f1"))
        self.assert_validation(fixture_payload(dependencies=[""]))
        self.assert_validation(fixture_payload(dependencies=[None]))
        self.assert_validation(fixture_payload(dependencies=["f1"]))  # self dependency

    def test_rejects_duplicate_dependencies(self) -> None:
        self.service.create_fixture(fixture_payload("base"))
        with self.assertRaises(ApiError) as ctx:
            self.create(dependencies=["base", "base"])
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual([f["id"] for f in self.service.list_fixtures()], ["base"])

    def test_missing_dependency_returns_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.create(dependencies=["ghost"])
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "fixture_not_found")
        self.assertEqual(self.service.list_fixtures(), [])

    def test_duplicate_id_returns_409(self) -> None:
        self.create()
        with self.assertRaises(ApiError) as ctx:
            self.create()
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "fixture_exists")

    # -- read / delete ---------------------------------------------------------

    def test_get_missing_returns_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.service.get_fixture("ghost")
        self.assertEqual(ctx.exception.code, "fixture_not_found")

    def test_delete_returns_204_semantics_and_removes_fixture(self) -> None:
        self.create()
        self.service.delete_fixture("f1")
        self.assertEqual(self.service.list_fixtures(), [])
        with self.assertRaises(ApiError) as ctx:
            self.service.delete_fixture("f1")
        self.assertEqual(ctx.exception.code, "fixture_not_found")

    def test_delete_blocked_by_dependent_fixture(self) -> None:
        self.create(fixture_payload("base"))
        self.create(fixture_payload("top", dependencies=["base"]))
        with self.assertRaises(ApiError) as ctx:
            self.service.delete_fixture("base")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "fixture_in_use")

    def test_delete_blocked_by_case_reference(self) -> None:
        self.create()
        self.service.create_case(case_payload(fixture_ids=["f1"]))
        with self.assertRaises(ApiError) as ctx:
            self.service.delete_fixture("f1")
        self.assertEqual(ctx.exception.code, "fixture_in_use")

    def test_delete_allowed_after_dependents_removed(self) -> None:
        self.create(fixture_payload("base"))
        self.create(fixture_payload("top", dependencies=["base"]))
        self.service.create_case(case_payload(fixture_ids=["base"]))
        self.service.delete_fixture("top")
        self.service.delete_case("c1")
        self.service.delete_fixture("base")
        self.assertEqual(self.service.list_fixtures(), [])


class CaseFixtureIdsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.create_suite({"id": "s1", "name": "S1"})
        self.service.create_fixture(fixture_payload("f1"))
        self.service.create_fixture(fixture_payload("f2"))

    def test_fixture_ids_omitted_when_not_provided(self) -> None:
        case = self.service.create_case(case_payload())
        self.assertNotIn("fixture_ids", case)
        self.assertNotIn("fixture_ids", self.service.get_case("c1"))
        self.assertNotIn("fixture_ids", self.service.list_cases({})[0])

    def test_explicit_empty_fixture_ids_preserved(self) -> None:
        case = self.service.create_case(case_payload(fixture_ids=[]))
        self.assertEqual(case["fixture_ids"], [])
        self.assertEqual(self.service.get_case("c1")["fixture_ids"], [])

    def test_fixture_ids_keep_declaration_order(self) -> None:
        case = self.service.create_case(case_payload(fixture_ids=["f2", "f1"]))
        self.assertEqual(case["fixture_ids"], ["f2", "f1"])

    def test_missing_fixture_returns_404_without_partial_data(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.service.create_case(case_payload(fixture_ids=["f1", "ghost"]))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "fixture_not_found")
        with self.assertRaises(ApiError):
            self.service.get_case("c1")

    def test_invalid_fixture_ids_return_400(self) -> None:
        for bad in ("f1", [""], [1], ["f1", "f1"]):
            with self.assertRaises(ApiError) as ctx:
                self.service.create_case(case_payload(fixture_ids=bad))
            self.assertEqual(ctx.exception.status, 400, bad)
            self.assertEqual(ctx.exception.code, "validation_error", bad)


class ExecutionPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.create_suite({"id": "s1", "name": "S1"})

    def add_fixture(self, fixture_id: str, **overrides: object) -> dict:
        return self.service.create_fixture(fixture_payload(fixture_id, **overrides))

    def add_case(self, **overrides: object) -> dict:
        return self.service.create_case(case_payload(**overrides))

    def test_missing_case_returns_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.service.get_execution_plan("ghost")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "case_not_found")

    def test_case_without_fixtures_has_empty_setup_and_teardown(self) -> None:
        self.add_case()
        plan = self.service.get_execution_plan("c1")
        self.assertEqual(plan["case_id"], "c1")
        self.assertEqual(plan["count"], 1)
        instance = plan["instances"][0]
        self.assertEqual(instance["id"], "c1[0]")
        self.assertEqual(instance["parameters"], {})
        self.assertEqual(instance["setup"], [])
        self.assertEqual(instance["steps"], [{"action": "run"}])
        self.assertEqual(instance["teardown"], [])

    def test_dependencies_come_first_and_teardown_is_reversed(self) -> None:
        self.add_fixture("db", setup_steps=[{"action": "db-up"}], teardown_steps=[{"action": "db-down"}])
        self.add_fixture("cache", setup_steps=[{"action": "cache-up"}], teardown_steps=[{"action": "cache-down"}])
        self.add_fixture(
            "app",
            setup_steps=[{"action": "app-up"}],
            teardown_steps=[{"action": "app-down"}],
            dependencies=["db", "cache"],
        )
        self.add_fixture("solo")
        self.add_case(fixture_ids=["app", "solo"])
        plan = self.service.get_execution_plan("c1")
        instance = plan["instances"][0]
        self.assertEqual(
            [item["fixture_id"] for item in instance["setup"]],
            ["db", "cache", "app", "solo"],
        )
        self.assertEqual(
            [item["fixture_id"] for item in instance["teardown"]],
            ["solo", "app", "cache", "db"],
        )
        self.assertEqual(instance["setup"][0], {"fixture_id": "db", "steps": [{"action": "db-up"}]})
        self.assertEqual(instance["teardown"][0], {"fixture_id": "solo", "steps": [{"action": "teardown"}]})

    def test_shared_dependency_appears_once(self) -> None:
        self.add_fixture("base")
        self.add_fixture("left", dependencies=["base"])
        self.add_fixture("right", dependencies=["base"])
        self.add_case(fixture_ids=["left", "right"])
        plan = self.service.get_execution_plan("c1")
        setup_ids = [item["fixture_id"] for item in plan["instances"][0]["setup"]]
        self.assertEqual(setup_ids, ["base", "left", "right"])

    def test_transitive_dependencies_follow_declaration_order(self) -> None:
        self.add_fixture("a")
        self.add_fixture("b", dependencies=["a"])
        self.add_fixture("c", dependencies=["b"])
        self.add_fixture("z")
        self.add_case(fixture_ids=["z", "c"])
        plan = self.service.get_execution_plan("c1")
        setup_ids = [item["fixture_id"] for item in plan["instances"][0]["setup"]]
        self.assertEqual(setup_ids, ["z", "a", "b", "c"])

    def test_every_parameterized_instance_carries_the_plan(self) -> None:
        self.add_fixture("f1")
        self.add_case(
            fixture_ids=["f1"],
            parameterization={"axes": {"env": ["dev", "prod"]}},
        )
        plan = self.service.get_execution_plan("c1")
        self.assertEqual(plan["count"], 2)
        self.assertEqual([i["id"] for i in plan["instances"]], ["c1[0]", "c1[1]"])
        self.assertEqual(plan["instances"][0]["parameters"], {"env": "dev"})
        self.assertEqual(plan["instances"][1]["parameters"], {"env": "prod"})
        for instance in plan["instances"]:
            self.assertEqual([i["fixture_id"] for i in instance["setup"]], ["f1"])
            self.assertEqual(instance["steps"], [{"action": "run"}])

    def test_disabled_case_still_previews(self) -> None:
        self.add_fixture("f1")
        self.add_case(enabled=False, fixture_ids=["f1"])
        plan = self.service.get_execution_plan("c1")
        self.assertEqual(plan["count"], 1)

    def test_preview_does_not_mutate_catalog(self) -> None:
        self.add_fixture("f1")
        self.add_case(fixture_ids=["f1"])
        before_case = self.service.get_case("c1")
        before_fixture = self.service.get_fixture("f1")
        plan = self.service.get_execution_plan("c1")
        plan["instances"][0]["setup"][0]["steps"].append({"action": "tamper"})
        plan["instances"][0]["steps"].append({"action": "tamper"})
        self.assertEqual(self.service.get_case("c1"), before_case)
        self.assertEqual(self.service.get_fixture("f1"), before_fixture)

    def test_deleted_case_loses_plan(self) -> None:
        self.add_case()
        self.service.delete_case("c1")
        with self.assertRaises(ApiError) as ctx:
            self.service.get_execution_plan("c1")
        self.assertEqual(ctx.exception.code, "case_not_found")


def make_handler(service: Service) -> type[Handler]:
    class _Handler(Handler):
        pass

    _Handler.service = service
    return _Handler


class ServerHarness:
    def __init__(self, service: Service | None = None) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service or Service()))
        self.host, self.port = self.httpd.server_address
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "ServerHarness":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def request(self, method: str, path: str, body: object = ...) -> tuple[int, dict]:
        conn = HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        payload = None
        if body is not ...:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data) if data else {}


class FixtureHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = ServerHarness()
        self.harness.__enter__()
        self.addCleanup(self.harness.__exit__, None, None, None)
        status, _ = self.harness.request("POST", "/v1/suites", {"id": "s1", "name": "S1"})
        self.assertEqual(status, 201)

    def test_fixture_crud_round_trip(self) -> None:
        status, body = self.harness.request("POST", "/v1/fixtures", fixture_payload())
        self.assertEqual(status, 201)
        self.assertEqual(body["id"], "f1")

        status, body = self.harness.request("GET", "/v1/fixtures/f1")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "fixture name")

        status, body = self.harness.request("GET", "/v1/fixtures")
        self.assertEqual(status, 200)
        self.assertEqual([f["id"] for f in body], ["f1"])

        status, body = self.harness.request("DELETE", "/v1/fixtures/f1")
        self.assertEqual(status, 204)
        self.assertEqual(body, {})

        status, body = self.harness.request("GET", "/v1/fixtures/f1")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "fixture_not_found")

    def test_fixture_validation_error_shape(self) -> None:
        status, body = self.harness.request(
            "POST", "/v1/fixtures", fixture_payload(setup_steps=[], teardown_steps=[])
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_fixture_conflict_and_in_use(self) -> None:
        self.harness.request("POST", "/v1/fixtures", fixture_payload())
        status, body = self.harness.request("POST", "/v1/fixtures", fixture_payload())
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "fixture_exists")

        status, _ = self.harness.request(
            "POST", "/v1/cases", case_payload(fixture_ids=["f1"])
        )
        self.assertEqual(status, 201)
        status, body = self.harness.request("DELETE", "/v1/fixtures/f1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "fixture_in_use")

    def test_case_with_missing_fixture_returns_404(self) -> None:
        status, body = self.harness.request(
            "POST", "/v1/cases", case_payload(fixture_ids=["ghost"])
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "fixture_not_found")

    def test_execution_plan_endpoint(self) -> None:
        self.harness.request("POST", "/v1/fixtures", fixture_payload("base"))
        self.harness.request(
            "POST", "/v1/fixtures", fixture_payload("top", dependencies=["base"])
        )
        self.harness.request(
            "POST",
            "/v1/cases",
            case_payload(fixture_ids=["top"], parameterization={"rows": [{"x": 1}, {"x": 2}]}),
        )
        status, body = self.harness.request("GET", "/v1/cases/c1/execution-plan")
        self.assertEqual(status, 200)
        self.assertEqual(body["case_id"], "c1")
        self.assertEqual(body["count"], 2)
        first = body["instances"][0]
        self.assertEqual([i["fixture_id"] for i in first["setup"]], ["base", "top"])
        self.assertEqual([i["fixture_id"] for i in first["teardown"]], ["top", "base"])
        self.assertEqual(first["steps"], [{"action": "run"}])

        status, body = self.harness.request("GET", "/v1/cases/ghost/execution-plan")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "case_not_found")

    def test_unknown_routes_still_return_not_found(self) -> None:
        status, body = self.harness.request("GET", "/v1/fixtures/f1/extra")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        status, body = self.harness.request("POST", "/v1/fixtures/f1", fixture_payload())
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
