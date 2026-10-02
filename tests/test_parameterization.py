"""Tests for parameterized case expansion and the instances preview."""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from testlattice.server import Handler
from testlattice.service import MAX_INSTANCES, ApiError, Service
from http.client import HTTPConnection


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


class ParameterizationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.service.create_suite({"id": "s1", "name": "S1"})

    def create(self, payload: dict | None = None, **overrides: object) -> dict:
        return self.service.create_case(payload if payload is not None else case_payload(**overrides))

    # -- axes ------------------------------------------------------------

    def test_axes_cartesian_product_rightmost_fastest(self) -> None:
        case = self.create(
            parameterization={"axes": {"a": [1, 2], "b": ["x", "y", "z"]}}
        )
        self.assertEqual(case["parameterization"], {"axes": {"a": [1, 2], "b": ["x", "y", "z"]}})
        preview = self.service.get_instances("c1")
        self.assertEqual(preview["case_id"], "c1")
        self.assertEqual(preview["count"], 6)
        self.assertEqual(
            [p["parameters"] for p in preview["instances"]],
            [
                {"a": 1, "b": "x"},
                {"a": 1, "b": "y"},
                {"a": 1, "b": "z"},
                {"a": 2, "b": "x"},
                {"a": 2, "b": "y"},
                {"a": 2, "b": "z"},
            ],
        )
        self.assertEqual(
            [p["id"] for p in preview["instances"]],
            ["c1[0]", "c1[1]", "c1[2]", "c1[3]", "c1[4]", "c1[5]"],
        )

    def test_axes_keep_declaration_order_single_axis(self) -> None:
        self.create(parameterization={"axes": {"browser": ["chromium", "firefox", "webkit"]}})
        preview = self.service.get_instances("c1")
        self.assertEqual(
            [p["parameters"]["browser"] for p in preview["instances"]],
            ["chromium", "firefox", "webkit"],
        )

    def test_axes_scalar_types_and_duplicates_kept(self) -> None:
        self.create(parameterization={"axes": {"v": ["a", 1, 1.5, True, False, None, "a"]}})
        preview = self.service.get_instances("c1")
        self.assertEqual(
            [p["parameters"]["v"] for p in preview["instances"]],
            ["a", 1, 1.5, True, False, None, "a"],
        )

    def test_axes_duplicate_combinations_kept(self) -> None:
        self.create(parameterization={"axes": {"a": [1, 1], "b": [2, 2]}})
        preview = self.service.get_instances("c1")
        self.assertEqual(preview["count"], 4)
        self.assertTrue(
            all(p["parameters"] == {"a": 1, "b": 2} for p in preview["instances"])
        )

    # -- rows ------------------------------------------------------------

    def test_rows_expand_in_input_order(self) -> None:
        case = self.create(
            parameterization={
                "rows": [
                    {"user": "alice", "retries": 0},
                    {"user": "bob", "retries": 3},
                ]
            }
        )
        self.assertEqual(
            case["parameterization"]["rows"],
            [{"user": "alice", "retries": 0}, {"user": "bob", "retries": 3}],
        )
        preview = self.service.get_instances("c1")
        self.assertEqual(preview["count"], 2)
        self.assertEqual(preview["instances"][0]["id"], "c1[0]")
        self.assertEqual(preview["instances"][0]["parameters"], {"user": "alice", "retries": 0})
        self.assertEqual(preview["instances"][1]["parameters"], {"user": "bob", "retries": 3})

    def test_rows_duplicate_rows_kept_and_key_order_normalized(self) -> None:
        self.create(
            parameterization={
                "rows": [
                    {"a": 1, "b": 2},
                    {"b": 3, "a": 4},
                    {"a": 1, "b": 2},
                ]
            }
        )
        preview = self.service.get_instances("c1")
        self.assertEqual(preview["count"], 3)
        self.assertEqual(
            [list(p["parameters"]) for p in preview["instances"]],
            [["a", "b"], ["a", "b"], ["a", "b"]],
        )
        self.assertEqual(preview["instances"][2]["parameters"], {"a": 1, "b": 2})

    # -- no parameterization ---------------------------------------------

    def test_without_parameterization_no_default_field(self) -> None:
        case = self.create()
        self.assertNotIn("parameterization", case)
        fetched = self.service.get_case("c1")
        self.assertNotIn("parameterization", fetched)
        self.assertEqual([c for c in self.service.list_cases({})][0].keys(), case.keys())

    def test_without_parameterization_preview_is_single_empty_instance(self) -> None:
        self.create(enabled=False)
        preview = self.service.get_instances("c1")
        self.assertEqual(
            preview,
            {
                "case_id": "c1",
                "count": 1,
                "instances": [{"id": "c1[0]", "parameters": {}}],
            },
        )

    def test_null_parameterization_is_treated_as_absent(self) -> None:
        case = self.create(parameterization=None)
        self.assertNotIn("parameterization", case)

    # -- echo + isolation ------------------------------------------------

    def test_parameterization_echoed_in_get_and_list(self) -> None:
        self.create(parameterization={"axes": {"x": [1]}})
        self.create(case_payload("c2", parameterization={"rows": [{"x": 1}]}))
        plain = self.create(case_payload("c3"))
        self.assertIn("parameterization", self.service.get_case("c1"))
        self.assertIn("parameterization", self.service.get_case("c2"))
        self.assertNotIn("parameterization", plain)
        echoed = {c["id"]: "parameterization" in c for c in self.service.list_cases({})}
        self.assertEqual(echoed, {"c1": True, "c2": True, "c3": False})

    def test_returned_data_is_detached(self) -> None:
        case = self.create(parameterization={"axes": {"v": [1, 2]}})
        case["parameterization"]["axes"]["v"].append(99)
        again = self.service.get_case("c1")
        self.assertEqual(again["parameterization"]["axes"]["v"], [1, 2])

        preview = self.service.get_instances("c1")
        preview["instances"][0]["parameters"]["v"] = "tampered"
        preview2 = self.service.get_instances("c1")
        self.assertEqual(preview2["instances"][0]["parameters"]["v"], 1)

    # -- validation ------------------------------------------------------

    def _expect_validation_error(self, parameterization: object) -> None:
        with self.assertRaises(ApiError) as caught:
            self.create(parameterization=parameterization)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "validation_error")

    def test_invalid_parameterization_payloads(self) -> None:
        for bad in (
            [],
            "axes",
            {},
            {"axes": {"a": [1]}, "rows": [{"a": 1}]},
            {"mode": "axes"},
            {"axes": {}},
            {"axes": {"1bad": [1]}},
            {"axes": {"a-b": [1]}},
            {"axes": {"a": []}},
            {"axes": {"a": "x"}},
            {"axes": {"a": [{}]}},
            {"axes": {"a": [[1]]}},
            {"rows": []},
            {"rows": {}},
            {"rows": [{}]},
            {"rows": ["x"]},
            {"rows": [{"a": 1}, {"a": 1, "b": 2}]},
            {"rows": [{"a": 1}, {"b": 2}]},
            {"rows": [{"1bad": 1}]},
            {"rows": [{"a": {"x": 1}}]},
            {"rows": [{"a": [1]}]},
        ):
            with self.subTest(bad=bad):
                self._expect_validation_error(bad)

    def test_invalid_parameterization_leaves_no_partial_case(self) -> None:
        self._expect_validation_error({"axes": {}})
        self.assertEqual(self.service.list_cases({}), [])
        # The id is free again after the rejected attempt.
        self.create(parameterization={"axes": {"a": [1]}})

    def test_instance_limit(self) -> None:
        boundary = MAX_INSTANCES
        self.create(parameterization={"axes": {"a": list(range(boundary))}})
        self.assertEqual(self.service.get_instances("c1")["count"], boundary)

        over_rows = [{"a": i} for i in range(MAX_INSTANCES + 1)]
        with self.assertRaises(ApiError) as caught:
            self.create(case_payload("c2", parameterization={"rows": over_rows}))
        self.assertEqual(caught.exception.code, "validation_error")
        with self.assertRaises(ApiError) as caught:
            self.create(
                case_payload(
                    "c3",
                    parameterization={"axes": {"a": list(range(501)), "b": [0, 1]}},
                )
            )
        self.assertEqual(caught.exception.code, "validation_error")

    def test_instances_for_missing_case_is_404(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.get_instances("ghost")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "case_not_found")

    def test_delete_makes_instances_unavailable(self) -> None:
        self.create(parameterization={"axes": {"a": [1]}})
        self.service.delete_case("c1")
        with self.assertRaises(ApiError) as caught:
            self.service.get_instances("c1")
        self.assertEqual(caught.exception.code, "case_not_found")


class ParameterizationHttpTest(unittest.TestCase):
    class Harness:
        def __init__(self, service: Service | None = None) -> None:
            class _Handler(Handler):
                pass

            _Handler.service = service or Service()
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            self.host, self.port = self.httpd.server_address
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

        def __enter__(self) -> "ParameterizationHttpTest.Harness":
            self.thread.start()
            return self

        def __exit__(self, *exc: object) -> None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join()

        def json(self, method: str, path: str, body: object = ...) -> tuple[int, object]:
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
            return response.status, json.loads(data) if data else None

    def test_instances_routes(self) -> None:
        with self.Harness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            status, created = server.json(
                "POST",
                "/v1/cases",
                case_payload(parameterization={"axes": {"os": ["linux", "win"], "n": [1, 2]}}),
            )
            self.assertEqual(status, 201)
            self.assertIn("parameterization", created)

            status, preview = server.json("GET", "/v1/cases/c1/instances")
            self.assertEqual(status, 200)
            self.assertEqual(preview["case_id"], "c1")
            self.assertEqual(preview["count"], 4)
            self.assertEqual(preview["instances"][0], {"id": "c1[0]", "parameters": {"os": "linux", "n": 1}})
            self.assertEqual(preview["instances"][3], {"id": "c1[3]", "parameters": {"os": "win", "n": 2}})

            # Plain case: one empty-parameters instance.
            server.json("POST", "/v1/cases", case_payload("c2"))
            status, preview = server.json("GET", "/v1/cases/c2/instances")
            self.assertEqual(status, 200)
            self.assertEqual(preview["instances"], [{"id": "c2[0]", "parameters": {}}])

            # Disabled case is still previewable.
            server.json("POST", "/v1/cases", case_payload("c3", enabled=False,
                                                          parameterization={"rows": [{"x": 0}]}))
            status, preview = server.json("GET", "/v1/cases/c3/instances")
            self.assertEqual(status, 200)
            self.assertEqual(preview["instances"][0]["parameters"], {"x": 0})

            # Preview does not mutate the catalog.
            _, case = server.json("GET", "/v1/cases/c1")
            self.assertEqual(case["parameterization"], {"axes": {"os": ["linux", "win"], "n": [1, 2]}})

            # Unknown case and malformed subpaths keep 404 behavior.
            status, body = server.json("GET", "/v1/cases/ghost/instances")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "case_not_found")
            status, body = server.json("GET", "/v1/cases/c1/other")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")
            status, body = server.json("GET", "/v1/cases/c1/instances/extra")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")

            # Deletion removes the preview endpoint too.
            status, _ = server.json("DELETE", "/v1/cases/c1")
            self.assertEqual(status, 204)
            status, body = server.json("GET", "/v1/cases/c1/instances")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "case_not_found")

    def test_invalid_parameterization_over_http(self) -> None:
        with self.Harness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            for bad in (
                {"axes": {"a": [1]}, "rows": [{"a": 1}]},
                {"axes": {}},
                {"rows": [{"a": 1}, {"b": 2}]},
            ):
                status, body = server.json(
                    "POST", "/v1/cases", case_payload(parameterization=bad)
                )
                self.assertEqual(status, 400, bad)
                self.assertEqual(body["error"]["code"], "validation_error", bad)
            _, cases = server.json("GET", "/v1/cases")
            self.assertEqual(cases, [])

            over = {"axes": {"a": list(range(501)), "b": [0, 1]}}
            status, body = server.json(
                "POST", "/v1/cases", case_payload(parameterization=over)
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
