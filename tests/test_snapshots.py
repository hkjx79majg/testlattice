"""Unit and HTTP tests for JSON snapshots and deterministic comparison."""

from __future__ import annotations

import json
import unittest

from testlattice.service import ApiError, Service
from testlattice.snapshots import compare_values, validate_compare_request

from test_http_catalog import ServerHarness


# -- unit: comparison -------------------------------------------------------


class CompareTest(unittest.TestCase):
    def test_identical_values_pass(self) -> None:
        for value in (
            None,
            True,
            False,
            0,
            1,
            1.5,
            "text",
            [1, "a", None, {"k": [True, 2]}],
            {"a": 1, "b": [1, 2], "c": None},
        ):
            with self.subTest(value=value):
                result = compare_values(value, json.loads(json.dumps(value)))
                self.assertTrue(result["passed"])
                self.assertEqual(
                    result["summary"],
                    {
                        "total": 0,
                        "value_mismatch": 0,
                        "missing_actual": 0,
                        "unexpected_actual": 0,
                    },
                )
                self.assertEqual(result["differences"], [])

    def test_object_member_differences(self) -> None:
        result = compare_values(
            {"a": 1, "b": 2, "c": 3},
            {"b": 2, "c": 4, "d": 5},
        )
        self.assertFalse(result["passed"])
        self.assertEqual(
            result["summary"],
            {
                "total": 3,
                "value_mismatch": 1,
                "missing_actual": 1,
                "unexpected_actual": 1,
            },
        )
        self.assertEqual(
            result["differences"],
            [
                {"path": "/a", "code": "missing_actual", "expected": 1},
                {"path": "/c", "code": "value_mismatch", "expected": 3, "actual": 4},
                {"path": "/d", "code": "unexpected_actual", "actual": 5},
            ],
        )

    def test_array_index_differences(self) -> None:
        result = compare_values([1, 2, 3], [1, 3, 4, 5])
        self.assertEqual(
            result["differences"],
            [
                {"path": "/1", "code": "value_mismatch", "expected": 2, "actual": 3},
                {"path": "/2", "code": "value_mismatch", "expected": 3, "actual": 4},
                {"path": "/3", "code": "unexpected_actual", "actual": 5},
            ],
        )

        result = compare_values([1, 2, 3, 4], [1])
        self.assertEqual(
            result["differences"],
            [
                {"path": "/1", "code": "missing_actual", "expected": 2},
                {"path": "/2", "code": "missing_actual", "expected": 3},
                {"path": "/3", "code": "missing_actual", "expected": 4},
            ],
        )

    def test_container_kind_mismatch_is_single_value_mismatch(self) -> None:
        result = compare_values({"a": 1}, [1])
        self.assertEqual(
            result["differences"],
            [
                {"path": "", "code": "value_mismatch", "expected": {"a": 1}, "actual": [1]},
            ],
        )
        result = compare_values([1, 2], {"0": 1, "1": 2})
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["differences"][0]["code"], "value_mismatch")

    def test_boolean_is_not_number_and_null_is_distinct(self) -> None:
        self.assertEqual(
            compare_values(True, 1)["differences"],
            [{"path": "", "code": "value_mismatch", "expected": True, "actual": 1}],
        )
        self.assertEqual(
            compare_values(False, 0)["differences"],
            [{"path": "", "code": "value_mismatch", "expected": False, "actual": 0}],
        )
        self.assertEqual(
            compare_values(None, 0)["differences"],
            [{"path": "", "code": "value_mismatch", "expected": None, "actual": 0}],
        )
        self.assertEqual(
            compare_values(None, False)["differences"],
            [{"path": "", "code": "value_mismatch", "expected": None, "actual": False}],
        )

    def test_object_key_order_does_not_matter(self) -> None:
        self.assertTrue(compare_values({"a": 1, "b": 2}, {"b": 2, "a": 1})["passed"])

    def test_keys_traversed_by_unicode_codepoint(self) -> None:
        # Code points: "A"(65), "a"(97), "é"(233), "中"(20013)
        expected = {"中": 1, "a": 1, "A": 1, "é": 1}
        actual = {"中": 2, "a": 2, "A": 2, "é": 2}
        result = compare_values(expected, actual)
        self.assertEqual([d["path"] for d in result["differences"]], ["/A", "/a", "/é", "/中"])

    def test_nested_paths_are_rfc6901_escaped(self) -> None:
        result = compare_values({"a/b": {"~x": 1}}, {"a/b": {"~x": 2}})
        self.assertEqual(result["differences"][0]["path"], "/a~1b/~0x")

    def test_nested_missing_members_interleave_in_key_order(self) -> None:
        result = compare_values({"b": {"x": 1}}, {"a": {"y": 2}})
        # Root container types match (both objects); members differ at root.
        self.assertEqual(
            result["differences"],
            [
                {"path": "/a", "code": "unexpected_actual", "actual": {"y": 2}},
                {"path": "/b", "code": "missing_actual", "expected": {"x": 1}},
            ],
        )

    def test_ignore_root_path(self) -> None:
        result = compare_values({"a": 1}, [9], frozenset([""]))
        self.assertTrue(result["passed"])
        self.assertEqual(result["differences"], [])

    def test_ignore_node_skips_descendants(self) -> None:
        expected = {"keep": 1, "skip": {"deep": {"x": 1}, "y": [1, 2]}}
        actual = {"keep": 1, "skip": {"deep": {"x": 99}, "y": [9, 8, 7]}}
        result = compare_values(expected, actual, frozenset({"/skip"}))
        self.assertTrue(result["passed"])

    def test_ignore_specific_paths_including_array_indices(self) -> None:
        expected = {"a": 1, "list": [1, 2, 3]}
        actual = {"a": 9, "list": [1, 8, 3, 4]}
        result = compare_values(expected, actual, frozenset({"/a", "/list/1", "/list/3"}))
        self.assertTrue(result["passed"])

    def test_ignore_nonexistent_path_has_no_effect(self) -> None:
        result = compare_values({"a": 1}, {"a": 2}, frozenset({"/ghost", "/a/nope"}))
        self.assertFalse(result["passed"])
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["differences"][0]["path"], "/a")

    def test_ignore_escaped_pointer(self) -> None:
        result = compare_values({"/": 1}, {"/": 2}, frozenset({"/~1"}))
        self.assertTrue(result["passed"])


# -- unit: request validation ----------------------------------------------


class ValidateCompareRequestTest(unittest.TestCase):
    def assert_invalid(self, payload: object) -> None:
        try:
            validate_compare_request(payload)
        except ApiError as error:
            self.assertEqual(error.status, 400)
            self.assertEqual(error.code, "validation_error")
            return
        raise AssertionError("expected validation_error")

    def test_accepts_actual_including_null(self) -> None:
        for value in (None, 0, False, "", [], {}):
            with self.subTest(value=value):
                actual, ignored = validate_compare_request({"actual": value})
                self.assertEqual(actual, value)
                self.assertEqual(ignored, frozenset())

    def test_ignore_paths_default_empty_or_explicit_empty(self) -> None:
        _, ignored = validate_compare_request({"actual": 1, "ignore_paths": []})
        self.assertEqual(ignored, frozenset())

    def test_invalid_payloads(self) -> None:
        self.assert_invalid([1, 2])
        self.assert_invalid("nope")
        self.assert_invalid({})
        self.assert_invalid({"expected": 1})
        self.assert_invalid({"actual": 1, "unknown": 2})
        self.assert_invalid({"actual": 1, "ignore_paths": {}})
        self.assert_invalid({"actual": 1, "ignore_paths": "/a"})
        self.assert_invalid({"actual": 1, "ignore_paths": [1]})
        self.assert_invalid({"actual": 1, "ignore_paths": ["a"]})
        self.assert_invalid({"actual": 1, "ignore_paths": ["~bad"]})
        self.assert_invalid({"actual": 1, "ignore_paths": ["/a", "/a"]})

    def test_valid_pointers_accepted(self) -> None:
        actual, ignored = validate_compare_request(
            {"actual": 1, "ignore_paths": ["", "/", "/a/b/0", "/a~1b/~0c", "/-"]}
        )
        self.assertEqual(ignored, frozenset({"", "/", "/a/b/0", "/a~1b/~0c", "/-"}))


# -- unit: service lifecycle ------------------------------------------------


class SnapshotServiceTest(unittest.TestCase):
    def test_lifecycle_and_id_reuse(self) -> None:
        service = Service()
        snapshot = service.create_snapshot({"id": "  s1  ", "value": None})
        self.assertEqual(snapshot, {"id": "s1", "value": None})
        self.assertEqual(service.list_snapshots(), [{"id": "s1", "value": None}])

        with self.assertRaises(ApiError) as ctx:
            service.create_snapshot({"id": "s1", "value": 1})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "snapshot_exists")

        service.delete_snapshot("s1")
        with self.assertRaises(ApiError) as ctx:
            service.get_snapshot("s1")
        self.assertEqual(ctx.exception.code, "snapshot_not_found")

        # Id is reusable after deletion.
        snapshot = service.create_snapshot({"id": "s1", "value": {"x": 1}})
        self.assertEqual(snapshot["value"], {"x": 1})

    def test_creation_order_preserved(self) -> None:
        service = Service()
        service.create_snapshot({"id": "a", "value": 1})
        service.create_snapshot({"id": "b", "value": 2})
        self.assertEqual([s["id"] for s in service.list_snapshots()], ["a", "b"])

    def test_stored_state_isolated_from_request_and_response(self) -> None:
        service = Service()
        request_value = {"nested": [1, 2]}
        service.create_snapshot({"id": "s", "value": request_value})
        request_value["nested"].append(3)
        fetched = service.get_snapshot("s")
        fetched["value"]["nested"].append(4)
        again = service.get_snapshot("s")
        self.assertEqual(again["value"], {"nested": [1, 2]})

    def test_failed_creation_leaves_no_data(self) -> None:
        service = Service()
        service.create_snapshot({"id": "s", "value": 1})
        for payload in ("nope", {"id": "x"}, {"value": 1}, {"id": "x", "value": 1, "extra": 2},
                        {"id": "  ", "value": 1}, {"id": 1, "value": 1}):
            with self.assertRaises(ApiError):
                service.create_snapshot(payload)
        self.assertEqual([s["id"] for s in service.list_snapshots()], ["s"])

    def test_compare_does_not_mutate_snapshot(self) -> None:
        service = Service()
        service.create_snapshot({"id": "s", "value": {"a": 1}})
        result = service.compare_snapshot("s", {"a": 2}, frozenset())
        self.assertFalse(result["passed"])
        self.assertEqual(service.get_snapshot("s")["value"], {"a": 1})

    def test_compare_missing_snapshot(self) -> None:
        service = Service()
        with self.assertRaises(ApiError) as ctx:
            service.compare_snapshot("ghost", 1, frozenset())
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "snapshot_not_found")


# -- HTTP -------------------------------------------------------------------


class SnapshotHttpTest(unittest.TestCase):
    def test_lifecycle_over_http(self) -> None:
        with ServerHarness() as server:
            status, created = server.json(
                "POST", "/v1/snapshots", {"id": "  snap1 ", "value": {"a": [1, True, None]}}
            )
            self.assertEqual(status, 201)
            self.assertEqual(created, {"id": "snap1", "value": {"a": [1, True, None]}})

            # null is a legal value.
            status, _ = server.json("POST", "/v1/snapshots", {"id": "nullish", "value": None})
            self.assertEqual(status, 201)

            status, listing = server.json("GET", "/v1/snapshots")
            self.assertEqual(status, 200)
            self.assertEqual([s["id"] for s in listing], ["snap1", "nullish"])

            status, fetched = server.json("GET", "/v1/snapshots/snap1")
            self.assertEqual(status, 200)
            self.assertEqual(fetched, created)

            status, _, data = server.request("DELETE", "/v1/snapshots/snap1")
            self.assertEqual(status, 204)
            self.assertEqual(data, b"")

            status, body = server.json("GET", "/v1/snapshots/snap1")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "snapshot_not_found")

            # Id reusable; list keeps creation order of the surviving + new entry.
            status, _ = server.json("POST", "/v1/snapshots", {"id": "snap1", "value": 1})
            self.assertEqual(status, 201)
            status, listing = server.json("GET", "/v1/snapshots")
            self.assertEqual([s["id"] for s in listing], ["nullish", "snap1"])

    def test_compare_over_http(self) -> None:
        with ServerHarness() as server:
            server.json(
                "POST",
                "/v1/snapshots",
                {"id": "s", "value": {"name": "x", "tags": ["a"], "meta": {"v": 1}}},
            )

            status, body = server.json(
                "POST", "/v1/snapshots/s/compare", {"actual": {"name": "x", "tags": ["a"], "meta": {"v": 1}}}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                body,
                {
                    "snapshot_id": "s",
                    "passed": True,
                    "summary": {
                        "total": 0,
                        "value_mismatch": 0,
                        "missing_actual": 0,
                        "unexpected_actual": 0,
                    },
                    "differences": [],
                },
            )

            status, body = server.json(
                "POST",
                "/v1/snapshots/s/compare",
                {
                    "actual": {"name": "y", "tags": ["a", "b"], "extra": 9},
                    "ignore_paths": ["/tags/1"],
                },
            )
            self.assertEqual(status, 200)
            self.assertFalse(body["passed"])
            self.assertEqual(
                body["summary"],
                {"total": 3, "value_mismatch": 1, "missing_actual": 1, "unexpected_actual": 1},
            )
            self.assertEqual(
                body["differences"],
                [
                    {"path": "/extra", "code": "unexpected_actual", "actual": 9},
                    {"path": "/meta", "code": "missing_actual", "expected": {"v": 1}},
                    {"path": "/name", "code": "value_mismatch", "expected": "x", "actual": "y"},
                ],
            )

            # Empty pointer ignores everything.
            status, body = server.json(
                "POST", "/v1/snapshots/s/compare", {"actual": 42, "ignore_paths": [""]}
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["passed"])

            # Compare did not modify the snapshot.
            _, fetched = server.json("GET", "/v1/snapshots/s")
            self.assertEqual(fetched["value"], {"name": "x", "tags": ["a"], "meta": {"v": 1}})

    def test_http_error_codes(self) -> None:
        with ServerHarness() as server:
            status, body = server.json("POST", "/v1/snapshots", raw_body=b"{bad")
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")
            status, body = server.json("POST", "/v1/snapshots/s/compare", raw_body=b"")
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

            for payload in ([1], {"id": "s"}, {"value": 1}, {"id": "s", "value": 1, "x": 2},
                            {"id": "   ", "value": 1}, {"id": 7, "value": 1}):
                status, body = server.json("POST", "/v1/snapshots", payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(body["error"]["code"], "validation_error", payload)

            server.json("POST", "/v1/snapshots", {"id": "s", "value": 1})
            status, body = server.json("POST", "/v1/snapshots", {"id": "s", "value": 2})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "snapshot_exists")

            for method, path in (
                ("GET", "/v1/snapshots/ghost"),
                ("DELETE", "/v1/snapshots/ghost"),
            ):
                status, body = server.json(method, path)
                self.assertEqual(status, 404, path)
                self.assertEqual(body["error"]["code"], "snapshot_not_found", path)

            for payload in (
                {},
                {"expected": 1},
                {"actual": 1, "extra": 2},
                {"actual": 1, "ignore_paths": "/a"},
                {"actual": 1, "ignore_paths": ["oops"]},
                {"actual": 1, "ignore_paths": ["/a", "/a"]},
                {"actual": 1, "ignore_paths": [None]},
            ):
                status, body = server.json("POST", "/v1/snapshots/s/compare", payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(body["error"]["code"], "validation_error", payload)

            status, body = server.json(
                "POST", "/v1/snapshots/ghost/compare", {"actual": 1}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "snapshot_not_found")

    def test_response_isolation_over_http(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/snapshots", {"id": "s", "value": {"a": 1}})
            _, first = server.json("GET", "/v1/snapshots/s")
            first["value"]["a"] = 999
            first["value"]["b"] = "mutated"
            _, second = server.json("GET", "/v1/snapshots/s")
            self.assertEqual(second["value"], {"a": 1})

    def test_restart_clears_snapshots(self) -> None:
        service = Service()
        with ServerHarness(service) as server:
            status, _ = server.json("POST", "/v1/snapshots", {"id": "s", "value": 1})
            self.assertEqual(status, 201)
        with ServerHarness(Service()) as server:
            status, listing = server.json("GET", "/v1/snapshots")
            self.assertEqual(status, 200)
            self.assertEqual(listing, [])


if __name__ == "__main__":
    unittest.main()
