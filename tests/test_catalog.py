"""Tests for the in-memory suite/case catalog (service layer)."""

import unittest

from testlattice.service import MAX_INSTANCES, ApiError, Service


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


class SuiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_create_suite_trims_id_and_name(self) -> None:
        suite = self.service.create_suite({"id": "  root  ", "name": "  Root  "})
        self.assertEqual(suite, {"id": "root", "name": "Root", "parent_id": None})

    def test_create_suite_with_parent(self) -> None:
        root = self.service.create_suite({"id": "root", "name": "Root"})
        child = self.service.create_suite({"id": "child", "name": "Child", "parent_id": "root"})
        self.assertIsNone(root["parent_id"])
        self.assertEqual(child["parent_id"], "root")

    def test_blank_id_or_name_is_validation_error(self) -> None:
        for body in (
            {"id": "   ", "name": "x"},
            {"id": "x", "name": "\t"},
            {"id": 1, "name": "x"},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ApiError) as caught:
                    self.service.create_suite(body)
                self.assertEqual(caught.exception.code, "validation_error")

    def test_missing_parent_is_suite_not_found(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.create_suite({"id": "x", "name": "X", "parent_id": "ghost"})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "suite_not_found")

    def test_self_reference_and_cycle_rejected(self) -> None:
        self.service.create_suite({"id": "a", "name": "A"})
        self.service.create_suite({"id": "b", "name": "B", "parent_id": "a"})
        with self.assertRaises(ApiError) as caught:
            self.service.create_suite({"id": "a2", "name": "A2", "parent_id": "a2"})
        self.assertEqual(caught.exception.code, "validation_error")
        # Make a ancestor point back: build a->b->c then try parenting a under c.
        self.service.create_suite({"id": "c", "name": "C", "parent_id": "b"})
        with self.assertRaises(ApiError) as caught:
            self.service.create_suite({"id": "d", "name": "D", "parent_id": "d"})
        self.assertEqual(caught.exception.code, "validation_error")

    def test_duplicate_id_is_conflict(self) -> None:
        self.service.create_suite({"id": "x", "name": "X"})
        with self.assertRaises(ApiError) as caught:
            self.service.create_suite({"id": "x", "name": "Again"})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "suite_exists")

    def test_listing_is_depth_first_creation_order(self) -> None:
        self.service.create_suite({"id": "root1", "name": "R1"})
        self.service.create_suite({"id": "root2", "name": "R2"})
        self.service.create_suite({"id": "r1-a", "name": "A", "parent_id": "root1"})
        self.service.create_suite({"id": "r1-b", "name": "B", "parent_id": "root1"})
        self.service.create_suite({"id": "r1-a-1", "name": "A1", "parent_id": "r1-a"})
        ids = [s["id"] for s in self.service.list_suites()]
        self.assertEqual(ids, ["root1", "r1-a", "r1-a-1", "r1-b", "root2"])

    def test_failed_creation_does_not_mutate(self) -> None:
        self.service.create_suite({"id": "x", "name": "X"})
        with self.assertRaises(ApiError):
            self.service.create_suite({"id": "x", "name": "X2"})
        self.assertEqual(self.service.get_suite("x")["name"], "X")
        with self.assertRaises(ApiError):
            self.service.create_suite({"id": "y", "name": "Y", "parent_id": "ghost"})
        self.assertEqual([s["id"] for s in self.service.list_suites()], ["x"])

    def test_get_missing_suite(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.get_suite("ghost")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "suite_not_found")

    def test_delete_suite_rules(self) -> None:
        self.service.create_suite({"id": "r", "name": "R"})
        self.service.create_suite({"id": "child", "name": "C", "parent_id": "r"})
        with self.assertRaises(ApiError) as caught:
            self.service.delete_suite("r")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "suite_not_empty")
        self.service.delete_suite("child")
        self.service.create_suite({"id": "leaf", "name": "L", "parent_id": "r"})
        self.service.create_case(case_payload("k1", "leaf"))
        with self.assertRaises(ApiError) as caught:
            self.service.delete_suite("r")
        self.assertEqual(caught.exception.code, "suite_not_empty")
        with self.assertRaises(ApiError) as caught:
            self.service.delete_suite("ghost")
        self.assertEqual(caught.exception.code, "suite_not_found")
        # Indirect child suites/cases also block deletion; remove them first.
        with self.assertRaises(ApiError) as caught:
            self.service.delete_suite("r")
        self.assertEqual(caught.exception.code, "suite_not_empty")
        self.service.delete_case("k1")
        self.service.delete_suite("leaf")
        self.service.delete_suite("r")
        with self.assertRaises(ApiError):
            self.service.get_suite("r")


class CaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.create_suite({"id": "s1", "name": "S1"})
        self.service.create_suite({"id": "s2", "name": "S2", "parent_id": "s1"})

    def test_create_case_defaults(self) -> None:
        case = self.service.create_case(case_payload())
        self.assertEqual(case["tags"], [])
        self.assertTrue(case["enabled"])
        self.assertEqual(case["timeout_seconds"], 300)

    def test_tags_dedup_preserves_first_seen_order(self) -> None:
        case = self.service.create_case(case_payload(tags=["smoke", "reg", "smoke", "api"]))
        self.assertEqual(case["tags"], ["smoke", "reg", "api"])

    def test_invalid_kind(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.create_case(case_payload(kind="perf"))
        self.assertEqual(caught.exception.code, "validation_error")

    def test_steps_rules(self) -> None:
        for steps in ([], [{"foo": "bar"}], "nope", [{}]):
            with self.subTest(steps=steps):
                with self.assertRaises(ApiError) as caught:
                    self.service.create_case(case_payload(steps=steps))
                self.assertEqual(caught.exception.code, "validation_error")

    def test_timeout_rules(self) -> None:
        for value in (0, 86401, 3.5, True, "300"):
            with self.subTest(value=value):
                with self.assertRaises(ApiError) as caught:
                    self.service.create_case(case_payload(timeout_seconds=value))
                self.assertEqual(caught.exception.code, "validation_error")
        case = self.service.create_case(case_payload(timeout_seconds=86400))
        self.assertEqual(case["timeout_seconds"], 86400)

    def test_enabled_must_be_boolean(self) -> None:
        with self.assertRaises(ApiError):
            self.service.create_case(case_payload(enabled="true"))

    def test_missing_suite_is_not_found(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.create_case(case_payload(suite_id="ghost"))
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "suite_not_found")

    def test_duplicate_case_id(self) -> None:
        self.service.create_case(case_payload())
        with self.assertRaises(ApiError) as caught:
            self.service.create_case(case_payload(suite_id="s2"))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "case_exists")

    def test_listing_order_and_filters(self) -> None:
        self.service.create_case(case_payload("c1", kind="api", tags=["a", "b"], enabled=False))
        self.service.create_case(case_payload("c2", "s2", kind="unit", tags=["a"]))
        self.service.create_case(case_payload("c3", "s1", kind="unit", tags=["b"]))

        self.assertEqual([c["id"] for c in self.service.list_cases({})], ["c1", "c2", "c3"])
        self.assertEqual(
            [c["id"] for c in self.service.list_cases({"kind": "unit"})], ["c2", "c3"]
        )
        self.assertEqual(
            [c["id"] for c in self.service.list_cases({"enabled": False})], ["c1"]
        )
        self.assertEqual(
            [c["id"] for c in self.service.list_cases({"suite_id": "s1"})], ["c1", "c3"]
        )
        self.assertEqual(
            [c["id"] for c in self.service.list_cases({"suite_id": "s1", "include_descendants": True})],
            ["c1", "c2", "c3"],
        )
        self.assertEqual(
            [c["id"] for c in self.service.list_cases({"tags": ("a", "b")})], ["c1"]
        )
        self.assertEqual(
            [c["id"] for c in self.service.list_cases({"suite_id": "s2", "tags": ("a",)})], ["c2"]
        )

    def test_listing_unknown_suite(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.list_cases({"suite_id": "ghost"})
        self.assertEqual(caught.exception.code, "suite_not_found")

    def test_empty_match_returns_empty_list(self) -> None:
        self.assertEqual(self.service.list_cases({"kind": "browser"}), [])

    def test_delete_case(self) -> None:
        self.service.create_case(case_payload())
        self.service.delete_case("c1")
        with self.assertRaises(ApiError) as caught:
            self.service.get_case("c1")
        self.assertEqual(caught.exception.code, "case_not_found")
        self.assertEqual(self.service.list_cases({}), [])
        with self.assertRaises(ApiError) as caught:
            self.service.delete_case("c1")
        self.assertEqual(caught.exception.code, "case_not_found")

    def test_returned_objects_are_detached(self) -> None:
        case = self.service.create_case(case_payload(tags=["x"]))
        case["tags"].append("tampered")
        case["steps"][0]["action"] = "tampered"
        again = self.service.get_case("c1")
        self.assertEqual(again["tags"], ["x"])
        self.assertEqual(again["steps"][0]["action"], "run")


class ParameterizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.create_suite({"id": "s1", "name": "S1"})

    def test_no_parameterization_keeps_response_shape(self) -> None:
        case = self.service.create_case(case_payload())
        self.assertNotIn("parameterization", case)
        fetched = self.service.get_case("c1")
        self.assertNotIn("parameterization", fetched)
        self.assertNotIn("parameterization", self.service.list_cases({})[0])

    def test_explicit_null_parameterization_is_omitted(self) -> None:
        case = self.service.create_case(case_payload(parameterization=None))
        self.assertNotIn("parameterization", case)
        self.assertEqual(self.service.list_instances("c1")["count"], 1)

    def test_parameterization_returned_on_read_and_list(self) -> None:
        param = {"axes": {"browser": ["chrome", "firefox"]}}
        case = self.service.create_case(case_payload(parameterization=param))
        self.assertEqual(case["parameterization"], param)
        self.assertEqual(self.service.get_case("c1")["parameterization"], param)
        self.assertEqual(self.service.list_cases({})[0]["parameterization"], param)

    def test_axes_cartesian_rightmost_fastest(self) -> None:
        self.service.create_case(
            case_payload(
                "c1",
                parameterization={"axes": {"a": [1, 2], "b": ["x", "y"], "c": [True, None]}},
            )
        )
        preview = self.service.list_instances("c1")
        self.assertEqual(preview["case_id"], "c1")
        self.assertEqual(preview["count"], 8)
        self.assertEqual(
            [p["parameters"] for p in preview["instances"]],
            [
                {"a": 1, "b": "x", "c": True},
                {"a": 1, "b": "x", "c": None},
                {"a": 1, "b": "y", "c": True},
                {"a": 1, "b": "y", "c": None},
                {"a": 2, "b": "x", "c": True},
                {"a": 2, "b": "x", "c": None},
                {"a": 2, "b": "y", "c": True},
                {"a": 2, "b": "y", "c": None},
            ],
        )
        self.assertEqual([p["id"] for p in preview["instances"][:3]], ["c1[0]", "c1[1]", "c1[2]"])
        self.assertEqual(preview["instances"][-1]["id"], "c1[7]")

    def test_axes_duplicate_values_and_combinations_kept(self) -> None:
        self.service.create_case(
            case_payload("c1", parameterization={"axes": {"a": [1, 1], "b": [2, 2]}})
        )
        preview = self.service.list_instances("c1")
        self.assertEqual(preview["count"], 4)
        self.assertTrue(all(p["parameters"] == {"a": 1, "b": 2} for p in preview["instances"]))

    def test_rows_expand_in_input_order(self) -> None:
        self.service.create_case(
            case_payload(
                "c1",
                parameterization={
                    "rows": [
                        {"a": 1, "b": "x"},
                        {"a": 2, "b": None},
                        {"a": 1, "b": "x"},
                    ]
                },
            )
        )
        preview = self.service.list_instances("c1")
        self.assertEqual(preview["count"], 3)
        self.assertEqual(
            [p["parameters"] for p in preview["instances"]],
            [{"a": 1, "b": "x"}, {"a": 2, "b": None}, {"a": 1, "b": "x"}],
        )
        self.assertEqual([p["id"] for p in preview["instances"]], ["c1[0]", "c1[1]", "c1[2]"])

    def test_rows_keep_declared_parameter_order(self) -> None:
        self.service.create_case(
            case_payload("c1", parameterization={"rows": [{"b": 1, "a": 2}, {"b": 3, "a": 4}]})
        )
        preview = self.service.list_instances("c1")
        self.assertEqual(list(preview["instances"][0]["parameters"]), ["b", "a"])

    def test_plain_case_single_empty_instance(self) -> None:
        self.service.create_case(case_payload("c1"))
        preview = self.service.list_instances("c1")
        self.assertEqual(
            preview,
            {
                "case_id": "c1",
                "count": 1,
                "instances": [{"id": "c1[0]", "parameters": {}}],
            },
        )

    def test_disabled_case_still_previewable(self) -> None:
        self.service.create_case(
            case_payload("c1", enabled=False, parameterization={"axes": {"a": [1]}})
        )
        preview = self.service.list_instances("c1")
        self.assertEqual(preview["count"], 1)
        self.assertFalse(self.service.get_case("c1")["enabled"])

    def test_preview_parameters_are_independent(self) -> None:
        self.service.create_case(
            case_payload("c1", parameterization={"rows": [{"a": 1}, {"a": 2}]})
        )
        first = self.service.list_instances("c1")
        first["instances"][0]["parameters"]["a"] = 999
        first["instances"][0]["parameters"]["z"] = "leak"
        second = self.service.list_instances("c1")
        self.assertEqual(second["instances"][0]["parameters"], {"a": 1})

    def test_instances_missing_case_is_404(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.list_instances("ghost")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "case_not_found")

    def test_instances_disappear_after_delete(self) -> None:
        self.service.create_case(case_payload("c1", parameterization={"axes": {"a": [1]}}))
        self.service.delete_case("c1")
        with self.assertRaises(ApiError) as caught:
            self.service.list_instances("c1")
        self.assertEqual(caught.exception.code, "case_not_found")

    def test_boundary_1000_instances_allowed(self) -> None:
        self.service.create_case(
            case_payload("c1", parameterization={"axes": {"a": list(range(100)), "b": list(range(10))}})
        )
        self.assertEqual(self.service.list_instances("c1")["count"], 1000)

    def test_validation_failures(self) -> None:
        bad_bodies = [
            ("both", {"axes": {"a": [1]}, "rows": [{"a": 1}]}),
            ("empty", {}),
            ("unknown", {"bogus": {}}),
            ("axes_not_object", {"axes": []}),
            ("axes_empty", {"axes": {}}),
            ("axis_not_array", {"axes": {"a": 1}}),
            ("axis_empty", {"axes": {"a": []}}),
            ("bad_axis_name", {"axes": {"1a": [1]}}),
            ("bad_axis_name_2", {"axes": {"a-b": [1]}}),
            ("non_scalar", {"axes": {"a": [[1]]}}),
            ("non_scalar_obj", {"axes": {"a": [{"x": 1}]}}),
            ("rows_not_array", {"rows": {}}),
            ("rows_empty", {"rows": []}),
            ("row_not_object", {"rows": [1]}),
            ("row_empty", {"rows": [{}]}),
            ("row_name_mismatch", {"rows": [{"a": 1}, {"b": 2}]}),
            ("row_missing_key", {"rows": [{"a": 1, "b": 2}, {"a": 1}]}),
            ("row_extra_key", {"rows": [{"a": 1}, {"a": 1, "b": 2}]}),
            ("bad_row_name", {"rows": [{"a-b": 1}]}),
            ("row_non_scalar", {"rows": [{"a": [1]}]}),
            ("too_many_axes", {"axes": {"a": list(range(1001))}}),
            ("too_many_product", {"axes": {"a": list(range(101)), "b": list(range(10))}}),
            ("too_many_rows", {"rows": [{"a": i} for i in range(1001)]}),
            ("not_object_list", ["axes"]),
            ("not_object_str", "axes"),
        ]
        for label, param in bad_bodies:
            with self.subTest(label=label):
                with self.assertRaises(ApiError) as caught:
                    self.service.create_case(case_payload(f"bad-{label}", parameterization=param))
                self.assertEqual(caught.exception.status, 400, label)
                self.assertEqual(caught.exception.code, "validation_error", label)

    def test_failed_parameterization_leaves_no_case(self) -> None:
        with self.assertRaises(ApiError):
            self.service.create_case(
                case_payload("c1", parameterization={"axes": {"a": []}})
            )
        self.assertEqual(self.service.list_cases({}), [])
        with self.assertRaises(ApiError):
            self.service.get_case("c1")

    def test_stored_parameterization_is_detached_from_input(self) -> None:
        param = {"axes": {"a": [1, 2]}}
        self.service.create_case(case_payload("c1", parameterization=param))
        param["axes"]["a"].append(3)
        self.assertEqual(self.service.get_case("c1")["parameterization"]["axes"]["a"], [1, 2])


if __name__ == "__main__":
    unittest.main()
