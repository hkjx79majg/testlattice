"""End-to-end HTTP tests for POST /v1/assertions/evaluate."""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from testlattice.server import Handler
from testlattice.service import Service


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

    def request(
        self, method: str, path: str, body: object = ..., raw_body: bytes | None = None
    ) -> tuple[int, dict | list]:
        conn = HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        payload: bytes | None = None
        if raw_body is not None:
            payload = raw_body
            headers["Content-Type"] = "application/json"
        elif body is not ...:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if payload is not None:
            headers["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        status = response.status
        conn.close()
        return status, json.loads(data)


def evaluate(server: ServerHarness, actual: object, assertions: list[dict]) -> tuple[int, object]:
    return server.request(
        "POST", "/v1/assertions/evaluate", {"actual": actual, "assertions": assertions}
    )


def assertion(assertion_id: str, operator: str, expected: object, path: str | None = None) -> dict:
    item = {"id": assertion_id, "operator": operator, "expected": expected}
    if path is not None:
        item["path"] = path
    return item


class AssertionSuccessTests(unittest.TestCase):
    def test_root_default_path_and_summary(self) -> None:
        with ServerHarness() as server:
            status, body = evaluate(server, {"a": 1}, [assertion("x", "equals", {"a": 1})])
            self.assertEqual(status, 200)
            self.assertEqual(
                body,
                {
                    "passed": True,
                    "summary": {"total": 1, "passed": 1, "failed": 0},
                    "results": [
                        {
                            "id": "x",
                            "path": "",
                            "operator": "equals",
                            "expected": {"a": 1},
                            "actual": {"a": 1},
                            "passed": True,
                            "code": "ok",
                        }
                    ],
                },
            )

    def test_mixed_results_keep_order_and_do_not_abort(self) -> None:
        with ServerHarness() as server:
            status, body = evaluate(
                server,
                {"n": 3, "s": "hello", "list": [1, {"x": 2}]},
                [
                    assertion("a1", "equals", 3, "/n"),
                    assertion("a2", "equals", 4, "/n"),
                    assertion("a3", "not_equals", 4, "/n"),
                    assertion("a4", "not_equals", 3, "/n"),
                    assertion("a5", "contains", "ell", "/s"),
                    assertion("a6", "contains", {"x": 2}, "/list"),
                    assertion("a7", "contains", 9, "/list"),
                    assertion("a8", "type", "object"),
                    assertion("a9", "type", "number", "/n"),
                ],
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["passed"], False)
            self.assertEqual(body["summary"], {"total": 9, "passed": 6, "failed": 3})
            self.assertEqual([r["id"] for r in body["results"]],
                             ["a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9"])
            by_id = {r["id"]: r for r in body["results"]}
            self.assertEqual(by_id["a1"]["code"], "ok")
            self.assertEqual(by_id["a2"]["code"], "value_mismatch")
            self.assertEqual(by_id["a3"]["code"], "ok")
            self.assertEqual(by_id["a4"]["code"], "value_mismatch")
            self.assertEqual(by_id["a5"]["code"], "ok")
            self.assertEqual(by_id["a6"]["code"], "ok")
            self.assertEqual(by_id["a7"]["code"], "value_mismatch")
            self.assertEqual(by_id["a8"]["code"], "ok")
            self.assertEqual(by_id["a9"]["code"], "ok")

    def test_deep_equality_semantics(self) -> None:
        with ServerHarness() as server:
            # Object member order is irrelevant.
            status, body = evaluate(
                server, {"a": 1, "b": 2}, [assertion("o", "equals", {"b": 2, "a": 1})]
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["results"][0]["passed"])

            # Array order is significant.
            _, body = evaluate(server, [1, 2, 3], [assertion("arr", "equals", [3, 2, 1])])
            self.assertFalse(body["results"][0]["passed"])
            self.assertEqual(body["results"][0]["code"], "value_mismatch")

            # Nested structures compare deeply.
            _, body = evaluate(
                server,
                {"k": [True, None, {"m": "s"}]},
                [assertion("deep", "equals", {"k": [True, None, {"m": "s"}]})],
            )
            self.assertTrue(body["results"][0]["passed"])

            # Booleans are not numbers in either direction.
            _, body = evaluate(server, True, [assertion("b1", "equals", 1)])
            self.assertFalse(body["results"][0]["passed"])
            _, body = evaluate(server, 1, [assertion("b2", "equals", True)])
            self.assertFalse(body["results"][0]["passed"])
            _, body = evaluate(server, False, [assertion("b3", "equals", 0)])
            self.assertFalse(body["results"][0]["passed"])

            # JSON numbers compare numerically.
            _, body = evaluate(server, 1, [assertion("n1", "equals", 1.0)])
            self.assertTrue(body["results"][0]["passed"])

            # Type mismatches at the top level are value mismatches for equals.
            _, body = evaluate(server, "1", [assertion("s", "equals", 1)])
            self.assertFalse(body["results"][0]["passed"])
            self.assertEqual(body["results"][0]["code"], "value_mismatch")

    def test_contains_semantics(self) -> None:
        with ServerHarness() as server:
            # Empty substring is contained.
            _, body = evaluate(server, "abc", [assertion("c1", "contains", "", )])
            self.assertEqual(body["results"][0]["code"], "ok")
            # Non-string expected against a string is a type mismatch.
            _, body = evaluate(server, "abc", [assertion("c2", "contains", 1)])
            self.assertEqual(body["results"][0]["code"], "type_mismatch")
            # Array membership is deep equality; string expected against array
            # is a type mismatch, not a false membership.
            _, body = evaluate(
                server,
                [1, 2, 3],
                [
                    assertion("c3", "contains", 2),
                    assertion("c4", "contains", 9),
                    assertion("c5", "contains", "2"),
                ],
            )
            codes = [r["code"] for r in body["results"]]
            self.assertEqual(codes, ["ok", "value_mismatch", "value_mismatch"])
            # contains on object/null/number is a type mismatch.
            _, body = evaluate(
                server,
                {"k": "v"},
                [
                    assertion("c6", "contains", "v"),
                    assertion("c7", "contains", 1, "/n"),
                ],
            )
            # /n is missing -> path_not_found takes precedence over operator
            self.assertEqual(
                [r["code"] for r in body["results"]], ["type_mismatch", "path_not_found"]
            )
            _, body = evaluate(server, None, [assertion("c8", "contains", None)])
            self.assertEqual(body["results"][0]["code"], "type_mismatch")

    def test_type_operator_all_names(self) -> None:
        cases = [
            (None, "null", True),
            (True, "boolean", True),
            (False, "boolean", True),
            (1, "number", True),
            (1.5, "number", True),
            ("x", "string", True),
            ([], "array", True),
            ({}, "object", True),
            (True, "number", False),
            (1, "boolean", False),
            ([], "object", False),
        ]
        with ServerHarness() as server:
            for value, name, should_pass in cases:
                _, body = evaluate(server, value, [assertion("t", "type", name)])
                result = body["results"][0]
                self.assertEqual(result["passed"], should_pass, (value, name))
                self.assertEqual(
                    result["code"], "ok" if should_pass else "type_mismatch", (value, name)
                )


class PointerTests(unittest.TestCase):
    def test_object_keys_array_indices_and_escapes(self) -> None:
        actual = {"a/b": {"~c": [10, 20]}, "": {"0": "root-empty-key"}}
        with ServerHarness() as server:
            _, body = evaluate(
                server,
                actual,
                [
                    assertion("p1", "equals", 20, "/a~1b/~0c/1"),
                    assertion("p2", "equals", "root-empty-key", "//0"),
                    assertion("p3", "equals", 10, "/a~1b/~0c/0"),
                ],
            )
            self.assertTrue(all(r["passed"] for r in body["results"]))
            self.assertEqual(
                [r["actual"] for r in body["results"]], [20, "root-empty-key", 10]
            )

    def test_path_not_found_cases(self) -> None:
        actual = {"arr": [1, 2], "obj": {}}
        with ServerHarness() as server:
            _, body = evaluate(
                server,
                actual,
                [
                    assertion("m1", "equals", 1, "/missing"),
                    assertion("m2", "equals", 1, "/arr/2"),
                    assertion("m3", "equals", 1, "/arr/-"),
                    assertion("m4", "equals", 1, "/arr/01"),
                    assertion("m5", "equals", 1, "/obj/k/0"),
                    assertion("m6", "type", "number", "/arr/9"),
                ],
            )
            self.assertTrue(all(r["code"] == "path_not_found" for r in body["results"]))
            self.assertTrue(all(r["passed"] is False for r in body["results"]))
            # actual is omitted from a missing-path result, but the rest echoes.
            m1 = body["results"][0]
            self.assertNotIn("actual", m1)
            self.assertEqual(m1["path"], "/missing")
            self.assertEqual(m1["expected"], 1)
            self.assertEqual(m1["operator"], "equals")

    def test_index_through_scalar_is_not_found(self) -> None:
        with ServerHarness() as server:
            _, body = evaluate(server, {"s": "abc"}, [assertion("x", "equals", "a", "/s/0")])
            self.assertEqual(body["results"][0]["code"], "path_not_found")


class ValidationTests(unittest.TestCase):
    def assert_validation_error(self, status: int, body: object) -> None:
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertNotIn("results", body)

    def test_invalid_json_stays_invalid_json(self) -> None:
        with ServerHarness() as server:
            status, body = server.request(
                "POST", "/v1/assertions/evaluate", raw_body=b"{nope"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

    def test_body_shape_errors(self) -> None:
        with ServerHarness() as server:
            for payload in ([], "x", 5, None, True):
                status, body = server.request(
                    "POST", "/v1/assertions/evaluate", body=payload
                )
                self.assert_validation_error(status, body)

            base = {"actual": 1, "assertions": [assertion("a", "equals", 1)]}
            for payload in (
                {"assertions": base["assertions"]},
                {"actual": 1},
                {"actual": 1, "assertions": [], "extra": 1},
            ):
                status, body = server.request(
                    "POST", "/v1/assertions/evaluate", body=payload
                )
                self.assert_validation_error(status, body)

    def test_assertion_array_bounds_and_shape(self) -> None:
        with ServerHarness() as server:
            for assertions in ([], [1], "x", {"id": "a"}):
                status, body = server.request(
                    "POST",
                    "/v1/assertions/evaluate",
                    body={"actual": None, "assertions": assertions},
                )
                self.assert_validation_error(status, body)

            too_many = [assertion(f"a{i}", "equals", 1) for i in range(1001)]
            status, body = server.request(
                "POST",
                "/v1/assertions/evaluate",
                body={"actual": 1, "assertions": too_many},
            )
            self.assert_validation_error(status, body)

            exactly_limit = [assertion(f"a{i}", "equals", 1) for i in range(1000)]
            status, body = server.request(
                "POST",
                "/v1/assertions/evaluate",
                body={"actual": 1, "assertions": exactly_limit},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["summary"]["total"], 1000)

    def test_assertion_field_errors(self) -> None:
        with ServerHarness() as server:
            bad_assertions: list[object] = [
                {"id": "", "operator": "equals", "expected": 1},
                {"id": 1, "operator": "equals", "expected": 1},
                {"id": "a", "operator": "eq", "expected": 1},
                {"id": "a", "expected": 1},
                {"id": "a", "operator": "equals"},
                {"id": "a", "operator": "equals", "expected": 1, "path": 2},
                {"id": "a", "operator": "equals", "expected": 1, "bogus": 0},
                {"id": "a", "operator": "type", "expected": "integer"},
                {"id": "a", "operator": "type", "expected": None},
                {"id": "a", "operator": "type", "expected": 1},
            ]
            for item in bad_assertions:
                status, body = server.request(
                    "POST",
                    "/v1/assertions/evaluate",
                    body={"actual": 1, "assertions": [item]},
                )
                self.assert_validation_error(status, body)

            # Duplicate ids reject the whole batch.
            status, body = server.request(
                "POST",
                "/v1/assertions/evaluate",
                body={
                    "actual": 1,
                    "assertions": [
                        assertion("same", "equals", 1),
                        assertion("other", "equals", 1),
                        assertion("same", "equals", 1),
                    ],
                },
            )
            self.assert_validation_error(status, body)

    def test_bad_pointer_syntax(self) -> None:
        with ServerHarness() as server:
            for pointer in ("a/b", "/~", "/~2", "/a~"):
                status, body = server.request(
                    "POST",
                    "/v1/assertions/evaluate",
                    body={
                        "actual": {"a/b": 1},
                        "assertions": [assertion("a", "equals", 1, pointer)],
                    },
                )
                self.assert_validation_error(status, body)


class RoutingAndStateTests(unittest.TestCase):
    def test_unknown_routes_unchanged(self) -> None:
        with ServerHarness() as server:
            status, body = server.request("GET", "/v1/assertions/evaluate")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")
            status, body = server.request("POST", "/v1/assertions", {"actual": 1})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")

    def test_evaluation_is_stateless(self) -> None:
        service = Service()
        with ServerHarness(service) as server:
            status, body = server.request(
                "POST",
                "/v1/suites",
                body={"id": "s1", "name": "S1"},
            )
            self.assertEqual(status, 201)
            evaluate(server, {"k": [1, 2]}, [assertion("a", "contains", 2, "/k")])
            evaluate(server, {}, [assertion("b", "equals", 1, "/missing")])
            # Catalog data is untouched and the endpoint keeps no assertion state.
            status, suites = server.request("GET", "/v1/suites")
            self.assertEqual(status, 200)
            self.assertEqual([s["id"] for s in suites], ["s1"])
            # Repeated calls are independent.
            status, first = evaluate(server, 1, [assertion("a", "equals", 1)])
            status, second = evaluate(server, 2, [assertion("a", "equals", 1)])
            self.assertTrue(first["passed"])
            self.assertFalse(second["passed"])
            self.assertEqual(first["summary"], {"total": 1, "passed": 1, "failed": 0})
            self.assertEqual(second["summary"], {"total": 1, "passed": 0, "failed": 1})


if __name__ == "__main__":
    unittest.main()
