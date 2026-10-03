"""End-to-end HTTP tests for the JUnit XML run export."""

from __future__ import annotations

import json
import unittest
import xml.etree.ElementTree as ET

from test_runs import ServerHarness, case_payload, seed_cases


def submit(server: ServerHarness, run_id: str, instance_id: str, outcome: str,
           duration_ms: int = 5, **extra: object) -> tuple[int, dict]:
    payload = {
        "instance_id": instance_id,
        "outcome": outcome,
        "duration_ms": duration_ms,
    }
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def finish(server: ServerHarness, run_id: str) -> None:
    status, _ = server.json("POST", f"/v1/runs/{run_id}/complete")
    assert status == 200


def properties_of(element: ET.Element) -> dict[str, str]:
    props = element.find("properties")
    assert props is not None
    return {p.get("name"): p.get("value") for p in props}


class JunitExportTest(unittest.TestCase):
    def test_completed_run_renders_deterministic_xml(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            submit(server, "r1", "c2[0]", "passed", 1500)
            submit(server, "r1", "c2[1]", "failed", 250,
                   details={"z": 1, "a": {"y": 2}})
            submit(server, "r1", "c1[0]", "error", 5,
                   details="boom & <x>")
            finish(server, "r1")
            status, before_report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)

            status, content_type, data = server.request(
                "GET", "/v1/runs/r1/junit.xml"
            )
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/xml; charset=utf-8")
            self.assertTrue(data.startswith(
                b'<?xml version="1.0" encoding="UTF-8"?>'
            ))
            self.assertTrue(data.decode("utf-8").endswith("</testsuite>"))

            expected = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<testsuite name="testlattice.r1" tests="3" failures="1" '
                'errors="1" skipped="0" time="1.755">\n'
                '  <testcase classname="c2" name="c2[0]" time="1.500">\n'
                '    <properties>\n'
                '      <property name="case_name" value="&quot;name-c2&quot;"/>\n'
                '      <property name="parameter.x" value="1"/>\n'
                '    </properties>\n'
                '  </testcase>\n'
                '  <testcase classname="c2" name="c2[1]" time="0.250">\n'
                '    <properties>\n'
                '      <property name="case_name" value="&quot;name-c2&quot;"/>\n'
                '      <property name="parameter.x" value="2"/>\n'
                '    </properties>\n'
                '    <failure message="failed">{"a":{"y":2},"z":1}</failure>\n'
                '  </testcase>\n'
                '  <testcase classname="c1" name="c1[0]" time="0.005">\n'
                '    <properties>\n'
                '      <property name="case_name" value="&quot;name-c1&quot;"/>\n'
                '    </properties>\n'
                '    <error message="error">"boom &amp; &lt;x&gt;"</error>\n'
                '  </testcase>\n'
                '</testsuite>'
            )
            self.assertEqual(data.decode("utf-8"), expected)

            # The same unchanged run exports byte-identical bytes.
            _, _, again = server.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(again, data)

            # Structure checks.
            root = ET.fromstring(data)
            self.assertEqual(root.tag, "testsuite")
            self.assertEqual(root.get("name"), "testlattice.r1")
            self.assertEqual(root.get("tests"), "3")
            self.assertEqual(root.get("failures"), "1")
            self.assertEqual(root.get("errors"), "1")
            self.assertEqual(root.get("skipped"), "0")
            self.assertEqual(root.get("time"), "1.755")
            # Normal runs carry no testsuite-level retry properties.
            self.assertIsNone(root.find("properties"))

            cases = root.findall("testcase")
            self.assertEqual(
                [c.get("name") for c in cases], ["c2[0]", "c2[1]", "c1[0]"]
            )
            self.assertEqual([c.get("classname") for c in cases],
                             ["c2", "c2", "c1"])
            self.assertEqual(properties_of(cases[0]),
                             {"case_name": '"name-c2"', "parameter.x": "1"})
            self.assertIsNone(cases[0].find("failure"))
            self.assertIsNone(cases[0].find("error"))
            self.assertIsNone(cases[0].find("skipped"))
            failure = cases[1].find("failure")
            self.assertIsNotNone(failure)
            self.assertEqual(failure.get("message"), "failed")
            self.assertEqual(failure.text, '{"a":{"y":2},"z":1}')
            error = cases[2].find("error")
            self.assertIsNotNone(error)
            self.assertEqual(error.get("message"), "error")
            # XML parser hands back the unescaped JSON string.
            self.assertEqual(json.loads(error.text), "boom & <x>")

            # Exporting is read-only.
            status, after_report = server.json("GET", "/v1/runs/r1")
            self.assertEqual(status, 200)
            self.assertEqual(after_report, before_report)

    def test_skipped_counts_and_empty_result_text(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(server, "r1", "c1[0]", "skipped", 3000)
            finish(server, "r1")

            status, _, data = server.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(status, 200)
            root = ET.fromstring(data)
            self.assertEqual(root.get("tests"), "1")
            self.assertEqual(root.get("skipped"), "1")
            self.assertEqual(root.get("failures"), "0")
            self.assertEqual(root.get("errors"), "0")
            self.assertEqual(root.get("time"), "3.000")
            case = root.find("testcase")
            self.assertEqual(case.get("time"), "3.000")
            skipped = case.find("skipped")
            self.assertIsNotNone(skipped)
            self.assertIsNone(skipped.get("message"))
            # No details: the serialized element has empty text.
            self.assertIn("<skipped></skipped>", data.decode("utf-8"))

    def test_parameter_order_and_non_ascii_details(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.json(
                "POST",
                "/v1/cases",
                case_payload(
                    "cm",
                    parameterization={"axes": {"zeta": [1], "alpha": [2]}},
                ),
            )
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["cm"]})
            submit(
                server,
                "r1",
                "cm[0]",
                "failed",
                0,
                details={"消息": "通过", "nested": {"b": 1, "a": 2}},
            )
            finish(server, "r1")

            status, _, data = server.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(status, 200)
            # Non-ASCII stays unescaped and the body is valid UTF-8.
            text = data.decode("utf-8")
            self.assertIn("消息", text)
            self.assertIn("通过", text)
            root = ET.fromstring(data)
            case = root.find("testcase")
            names = list(properties_of(case))
            self.assertEqual(names, ["case_name", "parameter.alpha",
                                     "parameter.zeta"])
            failure = case.find("failure")
            # Recursive Unicode code-point key sort: nested keys too.
            self.assertEqual(
                failure.text,
                '{"nested":{"a":2,"b":1},"消息":"通过"}',
            )

    def test_retry_run_carries_retry_properties(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            submit(server, "r1", "c1[0]", "failed", 10, details={"x": 1})
            finish(server, "r1")

            status, _ = server.json(
                "POST", "/v1/runs/r1/retry", {"id": "r2"}
            )
            self.assertEqual(status, 201)
            submit(server, "r2", "c1[0]", "passed", 20)
            finish(server, "r2")

            status, _, data = server.request("GET", "/v1/runs/r2/junit.xml")
            self.assertEqual(status, 200)
            root = ET.fromstring(data)
            suite_props = properties_of(root)
            self.assertEqual(
                list(suite_props), ["retry_of", "root_run_id", "attempt"]
            )
            self.assertEqual(suite_props["retry_of"], '"r1"')
            self.assertEqual(suite_props["root_run_id"], '"r1"')
            self.assertEqual(suite_props["attempt"], "1")
            self.assertEqual(root.get("tests"), "1")
            self.assertEqual(root.get("failures"), "0")
            self.assertEqual(root.get("time"), "0.020")

    def test_errors_remain_json(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

            # Unknown run.
            status, content_type, data = server.request(
                "GET", "/v1/runs/ghost/junit.xml"
            )
            self.assertEqual(status, 404)
            self.assertEqual(content_type, "application/json; charset=utf-8")
            body = json.loads(data)
            self.assertEqual(body["error"]["code"], "run_not_found")

            # Open (incomplete) run.
            status, content_type, data = server.request(
                "GET", "/v1/runs/r1/junit.xml"
            )
            self.assertEqual(status, 409)
            self.assertEqual(content_type, "application/json; charset=utf-8")
            body = json.loads(data)
            self.assertEqual(body["error"]["code"], "run_incomplete")

    def test_unknown_routes_still_not_found(self) -> None:
        with ServerHarness() as server:
            status, content_type, data = server.request(
                "GET", "/v1/runs/nope/junit.xml/extra"
            )
            self.assertEqual(status, 404)
            self.assertEqual(content_type, "application/json; charset=utf-8")
            self.assertEqual(json.loads(data)["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
