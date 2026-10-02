"""Unit and HTTP tests for POST /v1/assertions/evaluate."""

from __future__ import annotations

import json
import unittest

from testlattice.assertions import evaluate_assertions
from testlattice.service import ApiError, Service

from test_http_catalog import ServerHarness, case_payload


def evaluate(payload: object) -> dict:
    return evaluate_assertions(payload)


def assert_error(payload: object) -> None:
    try:
        evaluate(payload)
    except ApiError as error:
        assert error.status == 400
        assert error.code == "validation_error"
        return
    raise AssertionError("expected validation_error")


class EvaluateHappyPathTest(unittest.TestCase):
    def test_all_pass_summary_and_order(self) -> None:
        result = evaluate(
            {
                "actual": {"name": "abc", "items": [1, True, {"k": 1}], "n": 3},
                "assertions": [
                    {"id": "a1", "operator": "equals", "expected": {"name": "abc", "items": [1, True, {"k": 1}], "n": 3}},
                    {"id": "a2", "path": "/name", "operator": "contains", "expected": "b"},
                    {"id": "a3", "path": "/items", "operator": "contains", "expected": {"k": 1}},
                    {"id": "a4", "path": "/items/1", "operator": "type", "expected": "boolean"},
                    {"id": "a5", "path": "/n", "operator": "not_equals", "expected": 4},
                ],
            }
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["summary"], {"total": 5, "passed": 5, "failed": 0})
        self.assertEqual([item["id"] for item in result["results"]], ["a1", "a2", "a3", "a4", "a5"])
        for item in result["results"]:
            self.assertTrue(item["passed"])
            self.assertEqual(item["code"], "ok")

    def test_mixed_results_keep_order_and_continue_after_failure(self) -> None:
        result = evaluate(
            {
                "actual": {"x": 1},
                "assertions": [
                    {"id": "first", "path": "/x", "operator": "equals", "expected": 2},
                    {"id": "second", "path": "/missing", "operator": "equals", "expected": 1},
                    {"id": "third", "path": "/x", "operator": "type", "expected": "number"},
                ],
            }
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["summary"], {"total": 3, "passed": 1, "failed": 2})
        self.assertEqual([item["code"] for item in result["results"]],
                         ["value_mismatch", "path_not_found", "ok"])
        self.assertEqual([item["passed"] for item in result["results"]], [False, False, True])

    def test_result_echoes_fields_and_actual_only_when_resolved(self) -> None:
        result = evaluate(
            {
                "actual": {"s": "hi"},
                "assertions": [
                    {"id": "r1", "path": "/s", "operator": "equals", "expected": "hi"},
                    {"id": "r2", "path": "/nope", "operator": "equals", "expected": 0},
                ],
            }
        )
        ok_item, missing_item = result["results"]
        self.assertEqual(
            ok_item,
            {"id": "r1", "path": "/s", "operator": "equals",
             "expected": "hi", "actual": "hi", "passed": True, "code": "ok"},
        )
        self.assertEqual(
            missing_item,
            {"id": "r2", "path": "/nope", "operator": "equals",
             "expected": 0, "passed": False, "code": "path_not_found"},
        )
        self.assertNotIn("actual", missing_item)

    def test_default_path_is_root(self) -> None:
        result = evaluate(
            {"actual": [1, 2], "assertions": [{"id": "root", "operator": "type", "expected": "array"}]}
        )
        self.assertEqual(result["results"][0]["actual"], [1, 2])
        self.assertEqual(result["results"][0]["path"], "")


class DeepEqualityTest(unittest.TestCase):
    def test_boolean_differs_from_number(self) -> None:
        result = evaluate(
            {"actual": True, "assertions": [{"id": "b", "operator": "equals", "expected": 1}]}
        )
        self.assertEqual(result["results"][0]["code"], "value_mismatch")
        result = evaluate(
            {"actual": False, "assertions": [{"id": "b", "operator": "not_equals", "expected": 0}]}
        )
        # False and 0 are different JSON values, so not_equals passes.
        self.assertEqual(result["results"][0]["code"], "ok")

    def test_array_order_is_significant(self) -> None:
        result = evaluate(
            {"actual": [1, 2], "assertions": [{"id": "a", "operator": "equals", "expected": [2, 1]}]}
        )
        self.assertEqual(result["results"][0]["code"], "value_mismatch")

    def test_object_key_order_is_insignificant(self) -> None:
        result = evaluate(
            {
                "actual": {"a": 1, "b": [1, 2]},
                "assertions": [{"id": "o", "operator": "equals",
                                "expected": {"b": [1, 2], "a": 1}}],
            }
        )
        self.assertEqual(result["results"][0]["code"], "ok")

    def test_nested_structures_compare_deeply(self) -> None:
        result = evaluate(
            {"actual": {"a": {"b": None}},
             "assertions": [{"id": "n", "operator": "equals", "expected": {"a": {"b": None}}}]}
        )
        self.assertEqual(result["results"][0]["code"], "ok")
        result = evaluate(
            {"actual": {"a": {"b": None}},
             "assertions": [{"id": "n", "operator": "equals", "expected": {"a": {"b": False}}}]}
        )
        self.assertEqual(result["results"][0]["code"], "value_mismatch")


class ContainsTest(unittest.TestCase):
    def test_string_substring(self) -> None:
        result = evaluate(
            {"actual": "hello world",
             "assertions": [
                 {"id": "hit", "operator": "contains", "expected": "world"},
                 {"id": "miss", "operator": "contains", "expected": "xyz"},
                 {"id": "empty", "operator": "contains", "expected": ""},
             ]},
        )
        self.assertEqual([item["code"] for item in result["results"]],
                         ["ok", "value_mismatch", "ok"])

    def test_array_deep_element(self) -> None:
        result = evaluate(
            {"actual": [1, {"a": 1}, [2]],
             "assertions": [
                 {"id": "obj", "operator": "contains", "expected": {"a": 1}},
                 {"id": "nested", "operator": "contains", "expected": [2]},
                 {"id": "bool", "operator": "contains", "expected": True},
                 {"id": "missing", "operator": "contains", "expected": {"a": 2}},
             ]},
        )
        self.assertEqual([item["code"] for item in result["results"]],
                         ["ok", "ok", "value_mismatch", "value_mismatch"])

    def test_type_mismatch_combinations(self) -> None:
        result = evaluate(
            {"actual": {"s": "abc", "n": 3, "o": {"k": 1}, "z": None, "arr": ["x"]},
             "assertions": [
                 {"id": "str-num", "path": "/s", "operator": "contains", "expected": 1},
                 {"id": "num", "path": "/n", "operator": "contains", "expected": 3},
                 {"id": "obj", "path": "/o", "operator": "contains", "expected": "k"},
                 {"id": "null", "path": "/z", "operator": "contains", "expected": None},
                 {"id": "arr-str", "path": "/arr", "operator": "contains", "expected": "x"},
             ]},
        )
        self.assertEqual(
            [item["code"] for item in result["results"]],
            ["type_mismatch", "type_mismatch", "type_mismatch", "type_mismatch", "ok"],
        )


class TypeOperatorTest(unittest.TestCase):
    def test_each_type_name(self) -> None:
        cases = [
            (None, "null"),
            (True, "boolean"),
            (False, "boolean"),
            (1, "number"),
            (1.5, "number"),
            ("x", "string"),
            ([], "array"),
            ({}, "object"),
        ]
        result = evaluate(
            {
                "actual": [value for value, _ in cases],
                "assertions": [
                    {"id": f"t{index}", "path": f"/{index}", "operator": "type",
                     "expected": type_name}
                    for index, (_, type_name) in enumerate(cases)
                ],
            }
        )
        self.assertTrue(all(item["code"] == "ok" for item in result["results"]))

    def test_wrong_type_is_type_mismatch(self) -> None:
        result = evaluate(
            {"actual": "1", "assertions": [{"id": "t", "operator": "type", "expected": "number"}]}
        )
        self.assertEqual(result["results"][0]["code"], "type_mismatch")
        # Booleans report boolean, never number, despite being ints in Python.
        result = evaluate(
            {"actual": True, "assertions": [{"id": "t", "operator": "type", "expected": "number"}]}
        )
        self.assertEqual(result["results"][0]["code"], "type_mismatch")
        # A mismatch still echoes the resolved actual value.
        self.assertEqual(result["results"][0]["actual"], True)


class JsonPointerTest(unittest.TestCase):
    def test_object_keys_and_nesting(self) -> None:
        actual = {"a": {"b": {"c": 42}}}
        result = evaluate(
            {"actual": actual,
             "assertions": [{"id": "p", "path": "/a/b/c", "operator": "equals", "expected": 42}]}
        )
        self.assertEqual(result["results"][0]["code"], "ok")

    def test_array_indices(self) -> None:
        actual = {"items": [10, 20, 30]}
        result = evaluate(
            {"actual": actual,
             "assertions": [
                 {"id": "first", "path": "/items/0", "operator": "equals", "expected": 10},
                 {"id": "last", "path": "/items/2", "operator": "equals", "expected": 30},
                 {"id": "past", "path": "/items/3", "operator": "equals", "expected": 0},
                 {"id": "dash", "path": "/items/-", "operator": "equals", "expected": 0},
                 {"id": "leading-zero", "path": "/items/01", "operator": "equals", "expected": 0},
             ]},
        )
        self.assertEqual(
            [item["code"] for item in result["results"]],
            ["ok", "ok", "path_not_found", "path_not_found", "path_not_found"],
        )

    def test_index_into_non_array_and_key_on_scalar(self) -> None:
        result = evaluate(
            {"actual": {"o": {"1": "v"}, "s": "ab"},
             "assertions": [
                 {"id": "key-not-index", "path": "/o/1", "operator": "equals", "expected": "v"},
                 {"id": "scalar", "path": "/s/0", "operator": "equals", "expected": "a"},
             ]},
        )
        self.assertEqual([item["code"] for item in result["results"]], ["ok", "path_not_found"])

    def test_escaped_tokens(self) -> None:
        actual = {"a/b": 1, "m~n": 2, "~": 3, "/": 4, "": 5}
        result = evaluate(
            {"actual": actual,
             "assertions": [
                 {"id": "slash", "path": "/a~1b", "operator": "equals", "expected": 1},
                 {"id": "tilde", "path": "/m~0n", "operator": "equals", "expected": 2},
                 {"id": "only-tilde", "path": "/~0", "operator": "equals", "expected": 3},
                 {"id": "only-slash", "path": "/~1", "operator": "equals", "expected": 4},
                 {"id": "empty-key", "path": "/", "operator": "equals", "expected": 5},
             ]},
        )
        self.assertTrue(all(item["code"] == "ok" for item in result["results"]))

    def test_missing_key(self) -> None:
        result = evaluate(
            {"actual": {"a": 1},
             "assertions": [{"id": "m", "path": "/b/c", "operator": "equals", "expected": 0}]}
        )
        self.assertEqual(result["results"][0]["code"], "path_not_found")

    def test_invalid_pointers_rejected(self) -> None:
        for pointer in ("a", "/a~2", "/a~", "/~x"):
            assert_error(
                {"actual": {},
                 "assertions": [{"id": "x", "path": pointer,
                                 "operator": "equals", "expected": 0}]}
            )


class ValidationTest(unittest.TestCase):
    def test_request_shape_errors(self) -> None:
        for payload in (
            [],
            "x",
            1,
            None,
            {"assertions": []},
            {"actual": 1},
            {"actual": 1, "assertions": [], "extra": 2},
        ):
            assert_error(payload)

    def test_assertions_array_bounds(self) -> None:
        assert_error({"actual": 1, "assertions": []})
        assert_error({"actual": 1, "assertions": "x"})
        too_many = [
            {"id": f"a{index}", "operator": "equals", "expected": 0}
            for index in range(1001)
        ]
        assert_error({"actual": 1, "assertions": too_many})
        # Exactly the limit is accepted.
        at_limit = [
            {"id": f"a{index}", "operator": "equals", "expected": index}
            for index in range(1000)
        ]
        result = evaluate({"actual": None, "assertions": at_limit})
        self.assertEqual(result["summary"]["total"], 1000)

    def test_item_shape_errors(self) -> None:
        base = {"actual": 1}
        for assertions in (
            ["x"],
            [{"operator": "equals", "expected": 0}],  # missing id
            [{"id": "a", "expected": 0}],  # missing operator
            [{"id": "a", "operator": "equals"}],  # missing expected
            [{"id": "", "operator": "equals", "expected": 0}],
            [{"id": 1, "operator": "equals", "expected": 0}],
            [{"id": "a", "operator": "equals", "expected": 0, "bogus": 1}],
            [{"id": "a", "operator": "wat", "expected": 0}],
            [{"id": "a", "operator": "type", "expected": "int"}],
            [{"id": "a", "operator": "type", "expected": None}],
            [{"id": "a", "operator": "type", "expected": 1}],
            [{"id": "a", "operator": "equals", "expected": 0, "path": 5}],
        ):
            assert_error({**base, "assertions": assertions})

    def test_duplicate_ids_rejected(self) -> None:
        assert_error(
            {"actual": 1,
             "assertions": [
                 {"id": "a", "operator": "equals", "expected": 1},
                 {"id": "a", "operator": "equals", "expected": 2},
             ]}
        )

    def test_validation_failure_returns_no_partial_results(self) -> None:
        # A duplicated id later in the array invalidates the whole request even
        # though the earlier assertion would pass.
        assert_error(
            {"actual": 1,
             "assertions": [
                 {"id": "a", "operator": "equals", "expected": 1},
                 {"id": "a", "path": "/bad~2", "operator": "equals", "expected": 2},
             ]}
        )

    def test_null_actual_is_a_valid_value(self) -> None:
        result = evaluate(
            {"actual": None,
             "assertions": [{"id": "n", "operator": "equals", "expected": None}]}
        )
        self.assertEqual(result["results"][0]["code"], "ok")


class HttpEvaluateTest(unittest.TestCase):
    def test_evaluate_route(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST",
                "/v1/assertions/evaluate",
                {"actual": {"x": 1},
                 "assertions": [
                     {"id": "ok", "path": "/x", "operator": "equals", "expected": 1},
                     {"id": "bad", "path": "/x", "operator": "equals", "expected": 2},
                 ]},
            )
            self.assertEqual(status, 200)
            self.assertFalse(body["passed"])
            self.assertEqual(body["summary"], {"total": 2, "passed": 1, "failed": 1})

    def test_invalid_json_still_invalid_json(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST", "/v1/assertions/evaluate", raw_body=b"{not json"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

    def test_validation_error_shape(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST", "/v1/assertions/evaluate", {"actual": 1}
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
            self.assertNotIn("results", body)

    def test_evaluate_is_stateless_against_catalog(self) -> None:
        service = Service()
        with ServerHarness(service) as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.json(
                "POST",
                "/v1/cases",
                case_payload("c1", "s1", parameterization={"axes": {"p": [1, 2]}}),
            )
            status, _ = server.json(
                "POST",
                "/v1/assertions/evaluate",
                {"actual": {"suites": 1},
                 "assertions": [{"id": "a", "path": "/suites",
                                 "operator": "type", "expected": "number"}]},
            )
            self.assertEqual(status, 200)
            status, suites = server.json("GET", "/v1/suites")
            self.assertEqual(status, 200)
            self.assertEqual([s["id"] for s in suites], ["s1"])
            status, plan = server.json("GET", "/v1/cases/c1/execution-plan")
            self.assertEqual(status, 200)
            self.assertEqual(plan["count"], 2)

    def test_unknown_route_unchanged(self) -> None:
        with ServerHarness() as server:
            for method, path in (
                ("GET", "/v1/assertions/evaluate"),
                ("POST", "/v1/assertions"),
                ("POST", "/nope"),
            ):
                status, body = server.json(method, path, {})
                self.assertEqual(status, 404, path)
                self.assertEqual(body["error"]["code"], "not_found", path)


if __name__ == "__main__":
    unittest.main()
