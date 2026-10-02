import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from testlattice.server import Handler
from testlattice.service import Service


class CatalogHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        Handler.service = Service()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def request(self, method: str, path: str, body=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return resp.status, json.loads(payload) if payload else None, resp.headers
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            headers = exc.headers
            exc.close()
            return exc.code, json.loads(payload) if payload else None, headers

    def make_suite(self, suite_id: str, **extra):
        body = {"id": suite_id, "name": f"Suite {suite_id}"}
        body.update(extra)
        status, payload, _ = self.request("POST", "/v1/suites", body)
        self.assertEqual(status, 201, payload)
        return payload

    def make_case(self, case_id: str, suite_id: str, **extra):
        body = {
            "id": case_id,
            "name": f"Case {case_id}",
            "suite_id": suite_id,
            "kind": "unit",
            "steps": [{"action": "run"}],
        }
        body.update(extra)
        status, payload, _ = self.request("POST", "/v1/cases", body)
        self.assertEqual(status, 201, payload)
        return payload

    # -- baseline behavior -------------------------------------------------

    def test_health_and_unknown_route_unchanged(self):
        status, payload, headers = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("application/json", headers["Content-Type"])

        status, payload, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    # -- suites ------------------------------------------------------------

    def test_suite_create_get_delete(self):
        suite = self.make_suite("s1", parent_id=None)
        self.assertEqual(suite, {"id": "s1", "name": "Suite s1", "parent_id": None})

        status, payload, _ = self.request("GET", "/v1/suites/s1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], "s1")

        status, payload, headers = self.request("DELETE", "/v1/suites/s1")
        self.assertEqual(status, 204)
        self.assertIsNone(payload)

        status, payload, _ = self.request("GET", "/v1/suites/s1")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "suite_not_found")

    def test_suite_list_is_depth_first_creation_order(self):
        self.make_suite("b")
        self.make_suite("a")
        self.make_suite("b2", parent_id="b")
        self.make_suite("a1", parent_id="a")
        self.make_suite("b1", parent_id="b")
        self.make_suite("b1x", parent_id="b1")
        status, payload, _ = self.request("GET", "/v1/suites")
        self.assertEqual(status, 200)
        self.assertEqual([s["id"] for s in payload], ["b", "b2", "b1", "b1x", "a", "a1"])

    def test_suite_validation_errors(self):
        status, payload, _ = self.request("POST", "/v1/suites", raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        for body in (
            {"id": "x"},
            {"id": " ", "name": "n"},
            {"id": "x", "name": "  "},
            {"id": "x", "name": "n", "extra": 1},
            {"id": "x", "name": "n", "parent_id": 5},
        ):
            status, payload, _ = self.request("POST", "/v1/suites", body)
            self.assertEqual(status, 400, body)
            self.assertEqual(payload["error"]["code"], "validation_error", body)

        status, payload, _ = self.request("GET", "/v1/suites")
        self.assertEqual(payload, [])

    def test_suite_parent_rules(self):
        status, payload, _ = self.request(
            "POST", "/v1/suites", {"id": "s1", "name": "n", "parent_id": "ghost"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "suite_not_found")

        status, payload, _ = self.request(
            "POST", "/v1/suites", {"id": "s1", "name": "n", "parent_id": "s1"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "validation_error")

        self.make_suite("s1")
        status, payload, _ = self.request("POST", "/v1/suites", {"id": "s1", "name": "again"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "suite_exists")

    def test_suite_delete_not_empty(self):
        self.make_suite("root")
        self.make_suite("child", parent_id="root")
        status, payload, _ = self.request("DELETE", "/v1/suites/root")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "suite_not_empty")

        self.make_case("c1", "child")
        status, payload, _ = self.request("DELETE", "/v1/suites/child")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "suite_not_empty")

        self.request("DELETE", "/v1/cases/c1")
        status, _, _ = self.request("DELETE", "/v1/suites/child")
        self.assertEqual(status, 204)
        status, _, _ = self.request("DELETE", "/v1/suites/root")
        self.assertEqual(status, 204)

    # -- cases -------------------------------------------------------------

    def test_case_defaults_and_tag_dedup(self):
        self.make_suite("s1")
        case = self.make_case("c1", "s1", tags=["smoke", "fast", "smoke"])
        self.assertEqual(case["tags"], ["smoke", "fast"])
        self.assertIs(case["enabled"], True)
        self.assertEqual(case["timeout_seconds"], 300)

        status, payload, _ = self.request("GET", "/v1/cases/c1")
        self.assertEqual(status, 200)
        self.assertEqual(payload, case)

    def test_case_validation_errors(self):
        self.make_suite("s1")
        base = {"id": "c1", "name": "n", "suite_id": "s1", "kind": "unit",
                "steps": [{"action": "run"}]}
        bad = [
            {"id": "c1", "name": "n", "suite_id": "s1", "kind": "unit"},
            {**base, "kind": "load"},
            {**base, "steps": []},
            {**base, "steps": [{"action": " "}]},
            {**base, "steps": ["run"]},
            {**base, "tags": ["ok", ""]},
            {**base, "tags": "smoke"},
            {**base, "enabled": "yes"},
            {**base, "timeout_seconds": 0},
            {**base, "timeout_seconds": 86401},
            {**base, "timeout_seconds": 1.5},
            {**base, "timeout_seconds": True},
            {**base, "surprise": 1},
        ]
        for body in bad:
            status, payload, _ = self.request("POST", "/v1/cases", body)
            self.assertEqual(status, 400, body)
            self.assertEqual(payload["error"]["code"], "validation_error", body)

        status, payload, _ = self.request("GET", "/v1/cases")
        self.assertEqual(payload, [])

    def test_case_timeout_bounds_accepted(self):
        self.make_suite("s1")
        case = self.make_case("c1", "s1", timeout_seconds=1)
        self.assertEqual(case["timeout_seconds"], 1)
        case = self.make_case("c2", "s1", timeout_seconds=86400)
        self.assertEqual(case["timeout_seconds"], 86400)

    def test_case_missing_suite_and_duplicate(self):
        status, payload, _ = self.request(
            "POST", "/v1/cases",
            {"id": "c1", "name": "n", "suite_id": "ghost", "kind": "unit",
             "steps": [{"action": "run"}]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "suite_not_found")

        self.make_suite("s1")
        self.make_case("c1", "s1")
        status, payload, _ = self.request(
            "POST", "/v1/cases",
            {"id": "c1", "name": "n", "suite_id": "s1", "kind": "api",
             "steps": [{"action": "run"}]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "case_exists")

    def test_case_delete(self):
        self.make_suite("s1")
        self.make_case("c1", "s1")
        status, _, _ = self.request("DELETE", "/v1/cases/c1")
        self.assertEqual(status, 204)
        status, payload, _ = self.request("GET", "/v1/cases/c1")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "case_not_found")
        status, payload, _ = self.request("GET", "/v1/cases")
        self.assertEqual(payload, [])
        status, payload, _ = self.request("DELETE", "/v1/cases/c1")
        self.assertEqual(status, 404)

    # -- case filters --------------------------------------------------------

    def _seed_cases(self):
        self.make_suite("root")
        self.make_suite("child", parent_id="root")
        self.make_suite("grand", parent_id="child")
        self.make_case("c1", "root", kind="unit", tags=["smoke", "fast"])
        self.make_case("c2", "child", kind="api", enabled=False, tags=["smoke"])
        self.make_case("c3", "grand", kind="browser", tags=["slow"])

    def test_case_list_creation_order_and_filters(self):
        self._seed_cases()
        status, payload, _ = self.request("GET", "/v1/cases")
        self.assertEqual([c["id"] for c in payload], ["c1", "c2", "c3"])

        _, payload, _ = self.request("GET", "/v1/cases?suite_id=child")
        self.assertEqual([c["id"] for c in payload], ["c2"])

        _, payload, _ = self.request("GET", "/v1/cases?suite_id=root&include_descendants=true")
        self.assertEqual([c["id"] for c in payload], ["c1", "c2", "c3"])

        _, payload, _ = self.request("GET", "/v1/cases?suite_id=child&include_descendants=true")
        self.assertEqual([c["id"] for c in payload], ["c2", "c3"])

        _, payload, _ = self.request("GET", "/v1/cases?kind=api")
        self.assertEqual([c["id"] for c in payload], ["c2"])

        _, payload, _ = self.request("GET", "/v1/cases?enabled=false")
        self.assertEqual([c["id"] for c in payload], ["c2"])

        _, payload, _ = self.request("GET", "/v1/cases?tags=smoke,fast")
        self.assertEqual([c["id"] for c in payload], ["c1"])

        _, payload, _ = self.request("GET", "/v1/cases?tags=smoke")
        self.assertEqual([c["id"] for c in payload], ["c1", "c2"])

        _, payload, _ = self.request("GET", "/v1/cases?kind=unit&tags=slow")
        self.assertEqual(payload, [])

    def test_case_filter_errors(self):
        self._seed_cases()
        for path in (
            "/v1/cases?kind=load",
            "/v1/cases?enabled=maybe",
            "/v1/cases?include_descendants=yes&suite_id=root",
            "/v1/cases?include_descendants=true",
            "/v1/cases?tags=",
        ):
            status, payload, _ = self.request("GET", path)
            self.assertEqual(status, 400, path)
            self.assertEqual(payload["error"]["code"], "validation_error", path)

        status, payload, _ = self.request("GET", "/v1/cases?suite_id=ghost")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "suite_not_found")

    # -- response isolation ---------------------------------------------------

    def test_returned_objects_are_copies(self):
        self.make_suite("s1")
        case = self.make_case("c1", "s1", tags=["a"])
        case["steps"].append({"action": "mutated"})
        case["tags"].append("mutated")
        status, payload, _ = self.request("GET", "/v1/cases/c1")
        self.assertEqual(payload["steps"], [{"action": "run"}])
        self.assertEqual(payload["tags"], ["a"])


if __name__ == "__main__":
    unittest.main()
