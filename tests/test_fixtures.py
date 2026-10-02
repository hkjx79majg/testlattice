"""Tests for reusable fixtures, case fixture references and execution plans."""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from testlattice.server import Handler
from testlattice.service import ApiError, Service


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

    def request(self, method: str, path: str, body: object = ...) -> tuple[int, str, bytes]:
        conn = HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        payload: bytes | None = None
        if body is not ...:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        content_type = response.getheader("Content-Type", "")
        conn.close()
        return response.status, content_type, data

    def json(self, method: str, path: str, body: object = ...) -> tuple[int, dict | list]:
        status, _, data = self.request(method, path, body)
        return status, json.loads(data)


def fixture_payload(fixture_id: str = "f1", **overrides: object) -> dict:
    payload: dict = {
        "id": fixture_id,
        "name": "fixture",
        "setup_steps": [{"action": f"setup-{fixture_id}"}],
        "teardown_steps": [{"action": f"teardown-{fixture_id}"}],
    }
    payload.update(overrides)
    return payload


def case_payload(case_id: str = "c1", suite_id: str = "s1", **overrides: object) -> dict:
    payload: dict = {
        "id": case_id,
        "name": "case",
        "suite_id": suite_id,
        "kind": "unit",
        "steps": [{"action": "run"}],
    }
    payload.update(overrides)
    return payload


class FixtureServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_create_trims_id_and_name(self) -> None:
        fixture = self.service.create_fixture(
            fixture_payload("  f1  ", name="  Fix  ")
        )
        self.assertEqual(fixture["id"], "f1")
        self.assertEqual(fixture["name"], "Fix")

    def test_dependencies_kept_in_declared_order(self) -> None:
        self.service.create_fixture(fixture_payload("a"))
        self.service.create_fixture(fixture_payload("b"))
        fixture = self.service.create_fixture(
            fixture_payload("c", dependencies=["b", "a"])
        )
        self.assertEqual(fixture["dependencies"], ["b", "a"])
        self.assertEqual(self.service.get_fixture("c")["dependencies"], ["b", "a"])

    def test_dependencies_omitted_unless_provided(self) -> None:
        fixture = self.service.create_fixture(fixture_payload("f1"))
        self.assertNotIn("dependencies", fixture)
        self.assertNotIn("dependencies", self.service.get_fixture("f1"))
        self.assertNotIn("dependencies", self.service.list_fixtures()[0])

    def test_explicit_empty_dependencies_preserved(self) -> None:
        fixture = self.service.create_fixture(fixture_payload("f1", dependencies=[]))
        self.assertEqual(fixture["dependencies"], [])

    def test_validation_errors(self) -> None:
        bad_payloads = (
            {"name": "x", "setup_steps": [{"action": "a"}], "teardown_steps": []},
            fixture_payload("f1", extra=1),
            fixture_payload("f1", id="   "),
            fixture_payload("f1", name=""),
            fixture_payload("f1", setup_steps=[], teardown_steps=[]),
            fixture_payload("f1", setup_steps="nope"),
            fixture_payload("f1", setup_steps=[{"action": ""}]),
            fixture_payload("f1", setup_steps=[{"noaction": 1}]),
            fixture_payload("f1", setup_steps=["run"]),
            fixture_payload("f1", dependencies="f1"),
            fixture_payload("f1", dependencies=[""]),
            fixture_payload("f1", dependencies=[1]),
            fixture_payload("f1", dependencies=["f1"]),  # self dependency
        )
        for body in bad_payloads:
            with self.subTest(body=body):
                with self.assertRaises(ApiError) as ctx:
                    self.service.create_fixture(body)
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual(self.service.list_fixtures(), [])

    def test_duplicate_dependencies_rejected(self) -> None:
        self.service.create_fixture(fixture_payload("a"))
        with self.assertRaises(ApiError) as ctx:
            self.service.create_fixture(fixture_payload("b", dependencies=["a", "a"]))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_unknown_dependency_is_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.service.create_fixture(fixture_payload("b", dependencies=["ghost"]))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "fixture_not_found")
        self.assertEqual(self.service.list_fixtures(), [])

    def test_id_conflict_is_409(self) -> None:
        self.service.create_fixture(fixture_payload("f1"))
        with self.assertRaises(ApiError) as ctx:
            self.service.create_fixture(fixture_payload("f1", name="other"))
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "fixture_exists")

    def test_list_keeps_creation_order(self) -> None:
        for fixture_id in ("z", "a", "m"):
            self.service.create_fixture(fixture_payload(fixture_id))
        self.assertEqual(
            [f["id"] for f in self.service.list_fixtures()], ["z", "a", "m"]
        )

    def test_delete_missing_is_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.service.delete_fixture("ghost")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "fixture_not_found")

    def test_delete_blocked_by_dependent_fixture(self) -> None:
        self.service.create_fixture(fixture_payload("base"))
        self.service.create_fixture(fixture_payload("top", dependencies=["base"]))
        with self.assertRaises(ApiError) as ctx:
            self.service.delete_fixture("base")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "fixture_in_use")
        self.service.delete_fixture("top")
        self.service.delete_fixture("base")
        self.assertEqual(self.service.list_fixtures(), [])

    def test_delete_blocked_by_case_reference(self) -> None:
        self.service.create_suite({"id": "s1", "name": "S1"})
        self.service.create_fixture(fixture_payload("f1"))
        self.service.create_case(case_payload("c1", fixture_ids=["f1"]))
        with self.assertRaises(ApiError) as ctx:
            self.service.delete_fixture("f1")
        self.assertEqual(ctx.exception.code, "fixture_in_use")
        self.service.delete_case("c1")
        self.service.delete_fixture("f1")


class CaseFixtureRefsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.create_suite({"id": "s1", "name": "S1"})
        self.service.create_fixture(fixture_payload("f1"))
        self.service.create_fixture(fixture_payload("f2"))

    def test_fixture_ids_stored_in_declared_order(self) -> None:
        case = self.service.create_case(case_payload("c1", fixture_ids=["f2", "f1"]))
        self.assertEqual(case["fixture_ids"], ["f2", "f1"])
        self.assertEqual(self.service.get_case("c1")["fixture_ids"], ["f2", "f1"])

    def test_fixture_ids_omitted_unless_provided(self) -> None:
        case = self.service.create_case(case_payload("c1"))
        self.assertNotIn("fixture_ids", case)
        self.assertNotIn("fixture_ids", self.service.get_case("c1"))
        self.assertNotIn("fixture_ids", self.service.list_cases({})[0])

    def test_explicit_empty_fixture_ids_preserved(self) -> None:
        case = self.service.create_case(case_payload("c1", fixture_ids=[]))
        self.assertEqual(case["fixture_ids"], [])

    def test_invalid_fixture_ids(self) -> None:
        for value in ("f1", [""], [1], ["f1", "f1"]):
            with self.subTest(fixture_ids=value):
                with self.assertRaises(ApiError) as ctx:
                    self.service.create_case(case_payload("c1", fixture_ids=value))
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.code, "validation_error")
        with self.assertRaises(ApiError) as ctx:
            self.service.create_case(case_payload("c1", fixture_ids=["ghost"]))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "fixture_not_found")
        # Failed creations leave no partial case behind.
        with self.assertRaises(ApiError):
            self.service.get_case("c1")


class ExecutionPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.create_suite({"id": "s1", "name": "S1"})

    def create_fixture(self, fixture_id: str, dependencies: object = ...) -> dict:
        overrides: dict[str, object] = {}
        if dependencies is not ...:
            overrides["dependencies"] = dependencies
        return self.service.create_fixture(fixture_payload(fixture_id, **overrides))

    def test_plan_without_fixtures(self) -> None:
        self.service.create_case(case_payload("c1"))
        plan = self.service.get_execution_plan("c1")
        self.assertEqual(plan["case_id"], "c1")
        self.assertEqual(plan["count"], 1)
        (instance,) = plan["instances"]
        self.assertEqual(instance["id"], "c1[0]")
        self.assertEqual(instance["parameters"], {})
        self.assertEqual(instance["setup"], [])
        self.assertEqual(instance["steps"], [{"action": "run"}])
        self.assertEqual(instance["teardown"], [])

    def test_plan_orders_dependencies_first_and_reverses_teardown(self) -> None:
        self.create_fixture("base")
        self.create_fixture("mid", dependencies=["base"])
        self.create_fixture("extra", dependencies=["base"])
        self.create_fixture("top", dependencies=["mid", "extra"])
        self.service.create_case(case_payload("c1", fixture_ids=["top", "base"]))

        plan = self.service.get_execution_plan("c1")
        (instance,) = plan["instances"]
        self.assertEqual(
            [item["fixture_id"] for item in instance["setup"]],
            ["base", "mid", "extra", "top"],
        )
        self.assertEqual(
            [item["fixture_id"] for item in instance["teardown"]],
            ["top", "extra", "mid", "base"],
        )
        self.assertEqual(
            instance["setup"][0],
            {"fixture_id": "base", "steps": [{"action": "setup-base"}]},
        )
        self.assertEqual(
            instance["teardown"][0],
            {"fixture_id": "top", "steps": [{"action": "teardown-top"}]},
        )

    def test_plan_dedupes_shared_dependencies(self) -> None:
        self.create_fixture("shared")
        self.create_fixture("left", dependencies=["shared"])
        self.create_fixture("right", dependencies=["shared"])
        self.service.create_case(case_payload("c1", fixture_ids=["left", "right"]))
        plan = self.service.get_execution_plan("c1")
        (instance,) = plan["instances"]
        self.assertEqual(
            [item["fixture_id"] for item in instance["setup"]],
            ["shared", "left", "right"],
        )

    def test_plan_repeats_lifecycle_per_parameterized_instance(self) -> None:
        self.create_fixture("f1")
        self.service.create_case(
            case_payload(
                "c1",
                fixture_ids=["f1"],
                parameterization={"axes": {"env": ["dev", "prod"]}},
            )
        )
        plan = self.service.get_execution_plan("c1")
        self.assertEqual(plan["count"], 2)
        self.assertEqual(
            [i["parameters"] for i in plan["instances"]],
            [{"env": "dev"}, {"env": "prod"}],
        )
        for instance in plan["instances"]:
            self.assertEqual([i["fixture_id"] for i in instance["setup"]], ["f1"])
            self.assertEqual([i["fixture_id"] for i in instance["teardown"]], ["f1"])
            self.assertEqual(instance["steps"], [{"action": "run"}])

    def test_plan_available_for_disabled_case_and_does_not_mutate(self) -> None:
        self.create_fixture("f1")
        self.service.create_case(case_payload("c1", enabled=False, fixture_ids=["f1"]))
        before = self.service.get_case("c1")
        plan = self.service.get_execution_plan("c1")
        self.assertEqual(plan["count"], 1)
        self.assertEqual(self.service.get_case("c1"), before)

    def test_plan_missing_case_is_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.service.get_execution_plan("ghost")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "case_not_found")


class FixtureHttpTest(unittest.TestCase):
    def test_fixture_lifecycle_over_http(self) -> None:
        with ServerHarness() as server:
            status, created = server.json("POST", "/v1/fixtures", fixture_payload("f1"))
            self.assertEqual(status, 201)
            self.assertEqual(created["id"], "f1")

            status, fixture = server.json("GET", "/v1/fixtures/f1")
            self.assertEqual(status, 200)
            self.assertEqual(fixture, created)

            server.json("POST", "/v1/fixtures", fixture_payload("f2"))
            status, fixtures = server.json("GET", "/v1/fixtures")
            self.assertEqual(status, 200)
            self.assertEqual([f["id"] for f in fixtures], ["f1", "f2"])

            status, content_type, data = server.request("DELETE", "/v1/fixtures/f1")
            self.assertEqual(status, 204)
            self.assertEqual(data, b"")
            self.assertNotIn("application/json", content_type)

            status, body = server.json("GET", "/v1/fixtures/f1")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "fixture_not_found")
            status, body = server.json("DELETE", "/v1/fixtures/f1")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "fixture_not_found")

    def test_fixture_error_codes_over_http(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST", "/v1/fixtures", fixture_payload("f1", setup_steps=[])
            )
            # Only teardown steps remain, which is allowed.
            self.assertEqual(status, 201)

            status, body = server.json(
                "POST",
                "/v1/fixtures",
                fixture_payload("f2", setup_steps=[], teardown_steps=[]),
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

            status, body = server.json(
                "POST", "/v1/fixtures", fixture_payload("f2", dependencies=["ghost"])
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "fixture_not_found")

            status, body = server.json("POST", "/v1/fixtures", fixture_payload("f1"))
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "fixture_exists")

            _, fixtures = server.json("GET", "/v1/fixtures")
            self.assertEqual([f["id"] for f in fixtures], ["f1"])

    def test_execution_plan_over_http(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.json("POST", "/v1/fixtures", fixture_payload("f1"))
            server.json(
                "POST",
                "/v1/cases",
                case_payload("c1", fixture_ids=["f1"]),
            )
            status, plan = server.json("GET", "/v1/cases/c1/execution-plan")
            self.assertEqual(status, 200)
            self.assertEqual(plan["case_id"], "c1")
            self.assertEqual(plan["count"], 1)
            (instance,) = plan["instances"]
            self.assertEqual(
                instance["setup"],
                [{"fixture_id": "f1", "steps": [{"action": "setup-f1"}]}],
            )
            self.assertEqual(
                instance["teardown"],
                [{"fixture_id": "f1", "steps": [{"action": "teardown-f1"}]}],
            )

            status, body = server.json("GET", "/v1/cases/ghost/execution-plan")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "case_not_found")

            server.request("DELETE", "/v1/cases/c1")
            status, body = server.json("GET", "/v1/cases/c1/execution-plan")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "case_not_found")

    def test_fixture_in_use_over_http(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.json("POST", "/v1/fixtures", fixture_payload("f1"))
            server.json("POST", "/v1/cases", case_payload("c1", fixture_ids=["f1"]))
            status, body = server.json("DELETE", "/v1/fixtures/f1")
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "fixture_in_use")


if __name__ == "__main__":
    unittest.main()
