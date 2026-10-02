"""End-to-end HTTP tests for the JSON snapshot store and compare route."""

from __future__ import annotations

import json
import unittest

from test_http_catalog import ServerHarness


def create_snapshot(server: ServerHarness, snapshot_id: str, value: object) -> None:
    status, _ = server.json("POST", "/v1/snapshots", {"id": snapshot_id, "value": value})
    assert status == 201, f"setup create failed: {status}"


class SnapshotCrudTest(unittest.TestCase):
    def test_create_returns_201_and_object(self) -> None:
        with ServerHarness() as server:
            status, content_type, data = server.request(
                "POST", "/v1/snapshots", {"id": "s1", "value": {"a": [1, True, None]}}
            )
            self.assertEqual(status, 201)
            self.assertTrue(content_type.startswith("application/json"))
            self.assertEqual(
                json.loads(data), {"id": "s1", "value": {"a": [1, True, None]}}
            )

    def test_create_accepts_null_and_scalar_values(self) -> None:
        with ServerHarness() as server:
            for snapshot_id, value in (("n", None), ("num", 1.5), ("s", "x"), ("b", False)):
                status, body = server.json(
                    "POST", "/v1/snapshots", {"id": snapshot_id, "value": value}
                )
                self.assertEqual(status, 201)
                self.assertEqual(body, {"id": snapshot_id, "value": value})

    def test_id_is_trimmed_and_must_be_non_empty(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST", "/v1/snapshots", {"id": "  padded  ", "value": 1}
            )
            self.assertEqual(status, 201)
            self.assertEqual(body["id"], "padded")

            for bad_id in ("", "   ", 42, None):
                status, body = server.json(
                    "POST", "/v1/snapshots", {"id": bad_id, "value": 1}
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "validation_error")

    def test_create_validation_errors_leave_no_data(self) -> None:
        with ServerHarness() as server:
            bad_payloads = (
                {"id": "s1"},  # missing value
                {"value": 1},  # missing id
                {"id": "s1", "value": 1, "extra": 2},  # unknown field
                [1, 2],  # body not an object
                "text",
            )
            for payload in bad_payloads:
                status, body = server.json("POST", "/v1/snapshots", payload)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "validation_error")
            status, body = server.json("GET", "/v1/snapshots")
            self.assertEqual(status, 200)
            self.assertEqual(body, [])

    def test_malformed_json_returns_invalid_json(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST", "/v1/snapshots", raw_body=b'{"id": "s1",'
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")
            status, body = server.json("GET", "/v1/snapshots")
            self.assertEqual(body, [])

    def test_duplicate_id_returns_409(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", 1)
            status, body = server.json("POST", "/v1/snapshots", {"id": "s1", "value": 2})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "snapshot_exists")
            # The original snapshot is untouched.
            _, body = server.json("GET", "/v1/snapshots/s1")
            self.assertEqual(body["value"], 1)

    def test_list_preserves_creation_order(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "b", 2)
            create_snapshot(server, "a", 1)
            create_snapshot(server, "c", 3)
            status, body = server.json("GET", "/v1/snapshots")
            self.assertEqual(status, 200)
            self.assertEqual([item["id"] for item in body], ["b", "a", "c"])

    def test_get_missing_returns_404(self) -> None:
        with ServerHarness() as server:
            status, body = server.json("GET", "/v1/snapshots/nope")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "snapshot_not_found")

    def test_delete_returns_204_and_id_is_reusable(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"x": 1})
            status, content_type, data = server.request("DELETE", "/v1/snapshots/s1")
            self.assertEqual(status, 204)
            self.assertEqual(data, b"")
            self.assertNotIn("application/json", content_type)

            status, body = server.json("GET", "/v1/snapshots/s1")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "snapshot_not_found")

            status, body = server.json("DELETE", "/v1/snapshots/s1")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "snapshot_not_found")

            status, body = server.json("POST", "/v1/snapshots", {"id": "s1", "value": 9})
            self.assertEqual(status, 201)
            self.assertEqual(body["value"], 9)

    def test_responses_are_isolated_from_stored_state(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST", "/v1/snapshots", {"id": "s1", "value": {"a": [1]}}
            )
            body["value"]["a"].append(999)
            body["value"]["b"] = "polluted"

            status, body = server.json("GET", "/v1/snapshots/s1")
            self.assertEqual(body, {"id": "s1", "value": {"a": [1]}})
            body["value"]["a"].append(999)

            status, body = server.json("GET", "/v1/snapshots")
            self.assertEqual(body, [{"id": "s1", "value": {"a": [1]}}])


class SnapshotCompareTest(unittest.TestCase):
    def test_equal_values_pass_with_key_order_ignored(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"a": 1, "b": [True, None], "c": {"x": "y"}})
            status, body = server.json(
                "POST",
                "/v1/snapshots/s1/compare",
                {"actual": {"c": {"x": "y"}, "b": [True, None], "a": 1}},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["snapshot_id"], "s1")
            self.assertEqual(body["passed"], True)
            self.assertEqual(
                body["summary"],
                {"total": 0, "missing_actual": 0, "unexpected_actual": 0, "value_mismatch": 0},
            )
            self.assertEqual(body["differences"], [])

    def test_scalar_mismatch_echoes_both_sides(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"n": 1})
            status, body = server.json(
                "POST", "/v1/snapshots/s1/compare", {"actual": {"n": 2}}
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["passed"], False)
            self.assertEqual(
                body["differences"],
                [{"path": "/n", "code": "value_mismatch", "expected": 1, "actual": 2}],
            )
            self.assertEqual(body["summary"]["value_mismatch"], 1)

    def test_boolean_is_not_a_number(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"flag": True})
            _, body = server.json(
                "POST", "/v1/snapshots/s1/compare", {"actual": {"flag": 1}}
            )
            self.assertEqual(
                body["differences"],
                [{"path": "/flag", "code": "value_mismatch", "expected": True, "actual": 1}],
            )

    def test_container_kind_mismatch_reports_once_at_current_path(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"node": {"a": 1, "b": 2}})
            _, body = server.json(
                "POST", "/v1/snapshots/s1/compare", {"actual": {"node": [1, 2]}}
            )
            self.assertEqual(
                body["differences"],
                [
                    {
                        "path": "/node",
                        "code": "value_mismatch",
                        "expected": {"a": 1, "b": 2},
                        "actual": [1, 2],
                    }
                ],
            )

    def test_missing_and_unexpected_members(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"keep": 1, "gone": {"deep": True}})
            _, body = server.json(
                "POST",
                "/v1/snapshots/s1/compare",
                {"actual": {"keep": 1, "extra": [1, 2]}},
            )
            self.assertEqual(body["passed"], False)
            self.assertEqual(
                body["differences"],
                [
                    {"path": "/extra", "code": "unexpected_actual", "actual": [1, 2]},
                    {"path": "/gone", "code": "missing_actual", "expected": {"deep": True}},
                ],
            )
            self.assertEqual(
                body["summary"],
                {"total": 2, "missing_actual": 1, "unexpected_actual": 1, "value_mismatch": 0},
            )

    def test_keys_traverse_in_unicode_code_point_order(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"b": 1, "A": 2, "é": 3, "a": 4})
            _, body = server.json("POST", "/v1/snapshots/s1/compare", {"actual": {}})
            self.assertEqual(
                [d["path"] for d in body["differences"]],
                ["/A", "/a", "/b", "/é"],
            )

    def test_arrays_compare_by_index(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", [1, 2, 3])
            _, body = server.json(
                "POST", "/v1/snapshots/s1/compare", {"actual": [1, 9, 3, 4]}
            )
            self.assertEqual(
                body["differences"],
                [
                    {"path": "/1", "code": "value_mismatch", "expected": 2, "actual": 9},
                    {"path": "/3", "code": "unexpected_actual", "actual": 4},
                ],
            )
            _, body = server.json("POST", "/v1/snapshots/s1/compare", {"actual": [1]})
            self.assertEqual(
                body["differences"],
                [
                    {"path": "/1", "code": "missing_actual", "expected": 2},
                    {"path": "/2", "code": "missing_actual", "expected": 3},
                ],
            )

    def test_difference_paths_use_rfc6901_escaping(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"a/b": {"~key": 1}})
            _, body = server.json("POST", "/v1/snapshots/s1/compare", {"actual": {}})
            self.assertEqual(
                body["differences"],
                [
                    {
                        "path": "/a~1b",
                        "code": "missing_actual",
                        "expected": {"~key": 1},
                    }
                ],
            )
            _, body = server.json(
                "POST",
                "/v1/snapshots/s1/compare",
                {"actual": {"a/b": {"~key": 2}}},
            )
            self.assertEqual(
                body["differences"],
                [
                    {
                        "path": "/a~1b/~0key",
                        "code": "value_mismatch",
                        "expected": 1,
                        "actual": 2,
                    }
                ],
            )

    def test_ignore_paths_skip_subtrees(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"meta": {"ts": 1}, "data": [1, 2]})
            _, body = server.json(
                "POST",
                "/v1/snapshots/s1/compare",
                {
                    "actual": {"meta": {"ts": 2}, "data": [1, 3]},
                    "ignore_paths": ["/meta", "/data/1"],
                },
            )
            self.assertEqual(body["passed"], True)
            self.assertEqual(body["differences"], [])

    def test_empty_ignore_pointer_ignores_root(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"a": 1})
            _, body = server.json(
                "POST",
                "/v1/snapshots/s1/compare",
                {"actual": {"completely": "different"}, "ignore_paths": [""]},
            )
            self.assertEqual(body["passed"], True)

    def test_unmatched_ignore_paths_have_no_effect(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"a": 1})
            _, body = server.json(
                "POST",
                "/v1/snapshots/s1/compare",
                {"actual": {"a": 1}, "ignore_paths": ["/no/such/path", "/a/b"]},
            )
            self.assertEqual(body["passed"], True)

    def test_ignore_paths_default_and_empty_mean_no_ignoring(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"a": 1})
            for payload in ({"actual": {"a": 2}}, {"actual": {"a": 2}, "ignore_paths": []}):
                _, body = server.json("POST", "/v1/snapshots/s1/compare", payload)
                self.assertEqual(body["passed"], False)
                self.assertEqual(len(body["differences"]), 1)

    def test_compare_does_not_modify_snapshot(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", {"a": [1, 2]})
            server.json("POST", "/v1/snapshots/s1/compare", {"actual": {"a": [9]}})
            _, body = server.json("GET", "/v1/snapshots/s1")
            self.assertEqual(body, {"id": "s1", "value": {"a": [1, 2]}})

    def test_compare_missing_snapshot_returns_404(self) -> None:
        with ServerHarness() as server:
            status, body = server.json(
                "POST", "/v1/snapshots/nope/compare", {"actual": 1}
            )
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "snapshot_not_found")

    def test_compare_validation_errors(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", 1)
            bad_payloads = (
                {},  # missing actual
                {"actual": 1, "extra": 2},  # unknown field
                [1],  # body not an object
                {"actual": 1, "ignore_paths": "/a"},  # not an array
                {"actual": 1, "ignore_paths": [1]},  # non-string entry
                {"actual": 1, "ignore_paths": ["a"]},  # must start with /
                {"actual": 1, "ignore_paths": ["/a~"]},  # bad escape
                {"actual": 1, "ignore_paths": ["/a~2b"]},  # bad escape
                {"actual": 1, "ignore_paths": ["/a", "/a"]},  # duplicates
            )
            for payload in bad_payloads:
                status, body = server.json("POST", "/v1/snapshots/s1/compare", payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(body["error"]["code"], "validation_error")

    def test_compare_malformed_json_returns_invalid_json(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", 1)
            status, body = server.json(
                "POST", "/v1/snapshots/s1/compare", raw_body=b"{not json"
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_json")

    def test_compare_null_actual_against_null_snapshot(self) -> None:
        with ServerHarness() as server:
            create_snapshot(server, "s1", None)
            _, body = server.json("POST", "/v1/snapshots/s1/compare", {"actual": None})
            self.assertEqual(body["passed"], True)
            _, body = server.json("POST", "/v1/snapshots/s1/compare", {"actual": 0})
            self.assertEqual(
                body["differences"],
                [{"path": "", "code": "value_mismatch", "expected": None, "actual": 0}],
            )


class SnapshotRoutingTest(unittest.TestCase):
    def test_existing_routes_and_unknown_paths_unchanged(self) -> None:
        with ServerHarness() as server:
            status, body = server.json("GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "ok")

            status, body = server.json("GET", "/v1/snapshots/a/b")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")

            status, body = server.json("POST", "/v1/snapshots/a", {"id": "x", "value": 1})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")

            # Other catalog routes still work alongside snapshots.
            status, body = server.json("POST", "/v1/suites", {"id": "r", "name": "R"})
            self.assertEqual(status, 201)
            status, body = server.json("GET", "/v1/suites")
            self.assertEqual(len(body), 1)


if __name__ == "__main__":
    unittest.main()
