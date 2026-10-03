"""End-to-end HTTP tests for the JUnit XML run export."""

from __future__ import annotations

import json
import unittest
import xml.etree.ElementTree as ET

from testlattice.service import Service
from test_runs import ServerHarness, case_payload, seed_cases


def submit(server: ServerHarness, run_id: str, instance_id: str, outcome: str, **extra: object):
    payload = {"instance_id": instance_id, "outcome": outcome, "duration_ms": 5}
    payload.update(extra)
    return server.json("POST", f"/v1/runs/{run_id}/results", payload)


def finish(server: ServerHarness, run_id: str, results: dict[str, tuple[str, int]]) -> None:
    for instance_id, (outcome, duration_ms) in results.items():
        status, _ = submit(
            server, run_id, instance_id, outcome, duration_ms=duration_ms
        )
        assert status == 200, (instance_id, status)
    status, _ = server.json("POST", f"/v1/runs/{run_id}/complete")
    assert status == 200


class JUnitExportTest(unittest.TestCase):
    def test_completed_run_renders_full_document(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            submit(
                server,
                "r1",
                "c2[0]",
                "passed",
                duration_ms=12,
                details={"log": ["a", 1, None]},
            )
            submit(server, "r1", "c2[1]", "skipped", duration_ms=0)
            submit(
                server,
                "r1",
                "c1[0]",
                "failed",
                duration_ms=3,
                details={"z": 1, "a": {"y": 2, "x": [3, {"b": 1, "a": '中文<>&"'}]}},
            )
            server.json("POST", "/v1/runs/r1/complete")

            status, content_type, data = server.request(
                "GET", "/v1/runs/r1/junit.xml"
            )
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/xml; charset=utf-8")
            self.assertTrue(data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'))
            text = data.decode("utf-8")
            expected = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<testsuite name="testlattice.r1" tests="3" failures="1" '
                'errors="0" skipped="1" time="0.015">\n'
                '  <testcase classname="c2" name="c2[0]" time="0.012">\n'
                "    <properties>\n"
                '      <property name="case_name" value="&quot;name-c2&quot;"/>\n'
                '      <property name="parameter.x" value="1"/>\n'
                "    </properties>\n"
                "  </testcase>\n"
                '  <testcase classname="c2" name="c2[1]" time="0.000">\n'
                "    <properties>\n"
                '      <property name="case_name" value="&quot;name-c2&quot;"/>\n'
                '      <property name="parameter.x" value="2"/>\n'
                "    </properties>\n"
                "    <skipped></skipped>\n"
                "  </testcase>\n"
                '  <testcase classname="c1" name="c1[0]" time="0.003">\n'
                "    <properties>\n"
                '      <property name="case_name" value="&quot;name-c1&quot;"/>\n'
                "    </properties>\n"
                '    <failure message="failed">'
                '{"a":{"x":[3,{"a":"中文&lt;&gt;&amp;\\"","b":1}],"y":2},"z":1}'
                "</failure>\n"
                "  </testcase>\n"
                "</testsuite>\n"
            )
            self.assertEqual(text, expected)

            # The document parses as XML with a testsuite root.
            root = ET.fromstring(data)
            self.assertEqual(root.tag, "testsuite")
            self.assertEqual(root.get("name"), "testlattice.r1")

            # Unchanged run => byte-for-byte identical body on repeat export.
            status, _, again = server.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(status, 200)
            self.assertEqual(again, data)

    def test_passed_cases_have_no_result_element_and_no_suite_properties(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": ("passed", 1500)})
            status, _, data = server.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(status, 200)
            root = ET.fromstring(data)
            self.assertEqual(root.get("tests"), "1")
            self.assertEqual(root.get("failures"), "0")
            self.assertEqual(root.get("errors"), "0")
            self.assertEqual(root.get("skipped"), "0")
            self.assertEqual(root.get("time"), "1.500")
            # Normal runs carry no testsuite-level properties block.
            self.assertIsNone(root.find("properties"))
            case = root.find("testcase")
            self.assertIsNotNone(case)
            self.assertEqual(case.get("time"), "1.500")
            self.assertIsNone(case.find("failure"))
            self.assertIsNone(case.find("error"))
            self.assertIsNone(case.find("skipped"))

    def test_error_outcome_and_missing_details(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})
            finish(server, "r1", {"c1[0]": ("error", 1)})
            status, _, data = server.request("GET", "/v1/runs/r1/junit.xml")
            self.assertEqual(status, 200)
            text = data.decode("utf-8")
            self.assertIn('    <error message="error"></error>\n', text)
            root = ET.fromstring(data)
            error = root.find("testcase/error")
            self.assertIsNotNone(error)
            self.assertEqual(error.get("message"), "error")
            # ElementTree exposes an empty element's text as None.
            self.assertFalse(error.text)
            self.assertEqual(root.get("errors"), "1")
            self.assertEqual(root.get("failures"), "0")
            self.assertEqual(root.get("time"), "0.001")

    def test_parameter_order_and_compact_json_values(self) -> None:
        with ServerHarness() as server:
            server.json("POST", "/v1/suites", {"id": "s1", "name": "S1"})
            server.json(
                "POST",
                "/v1/cases",
                case_payload(
                    "cp",
                    name='q<a>&b"c',
                    parameterization={
                        "rows": [
                            {"b": 1, "A": "x", "_c": True, "a": None, "B": 2.5}
                        ]
                    },
                ),
            )
            server.json("POST", "/v1/runs", {"id": "r9", "case_ids": ["cp"]})
            finish(server, "r9", {"cp[0]": ("passed", 7)})
            _, _, data = server.request("GET", "/v1/runs/r9/junit.xml")
            root = ET.fromstring(data)
            names = [p.get("name") for p in root.findall("testcase/properties/property")]
            # case_name first, then parameter names by Unicode code point:
            # A(65), B(66), _(95), a(97), b(98)
            self.assertEqual(
                names,
                [
                    "case_name",
                    "parameter.A",
                    "parameter.B",
                    "parameter._c",
                    "parameter.a",
                    "parameter.b",
                ],
            )
            values = {
                p.get("name"): p.get("value")
                for p in root.findall("testcase/properties/property")
            }
            self.assertEqual(values["case_name"], '"q<a>&b\\"c"')
            self.assertEqual(values["parameter.A"], '"x"')
            self.assertEqual(values["parameter.B"], "2.5")
            self.assertEqual(values["parameter._c"], "true")
            self.assertEqual(values["parameter.a"], "null")
            self.assertEqual(values["parameter.b"], "1")

    def test_skipped_counts_toward_tests_only_and_time_is_sum(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish(
                server,
                "r1",
                {"c2[0]": ("skipped", 10), "c2[1]": ("skipped", 20), "c1[0]": ("passed", 30)},
            )
            _, _, data = server.request("GET", "/v1/runs/r1/junit.xml")
            root = ET.fromstring(data)
            self.assertEqual(root.get("tests"), "3")
            self.assertEqual(root.get("skipped"), "2")
            self.assertEqual(root.get("failures"), "0")
            self.assertEqual(root.get("errors"), "0")
            self.assertEqual(root.get("time"), "0.060")
            self.assertEqual(len(root.findall("testcase/skipped")), 2)
            case_times = [c.get("time") for c in root.findall("testcase")]
            self.assertEqual(case_times, ["0.010", "0.020", "0.030"])

    def test_retry_run_carries_retry_properties(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish(
                server,
                "r1",
                {"c2[0]": ("passed", 1), "c2[1]": ("failed", 2), "c1[0]": ("error", 4)},
            )
            status, _ = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            finish(server, "r2", {"c2[1]": ("failed", 8), "c1[0]": ("passed", 16)})

            _, _, data = server.request("GET", "/v1/runs/r2/junit.xml")
            text = data.decode("utf-8")
            expected_prefix = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<testsuite name="testlattice.r2" tests="2" failures="1" '
                'errors="0" skipped="0" time="0.024">\n'
                "  <properties>\n"
                '    <property name="retry_of" value="&quot;r1&quot;"/>\n'
                '    <property name="root_run_id" value="&quot;r1&quot;"/>\n'
                '    <property name="attempt" value="1"/>\n'
                "  </properties>\n"
            )
            self.assertTrue(text.startswith(expected_prefix), text)
            root = ET.fromstring(data)
            suite_props = {
                p.get("name"): p.get("value") for p in root.findall("properties/property")
            }
            self.assertEqual(
                suite_props,
                {"retry_of": '"r1"', "root_run_id": '"r1"', "attempt": "1"},
            )
            self.assertEqual(
                [c.get("name") for c in root.findall("testcase")],
                ["c2[1]", "c1[0]"],
            )

            # Chained retry keeps the root run id and bumps attempt.
            status, _ = server.json("POST", "/v1/runs/r2/retry", {"id": "r3"})
            self.assertEqual(status, 201)
            finish(server, "r3", {"c2[1]": ("failed", 2)})
            _, _, data = server.request("GET", "/v1/runs/r3/junit.xml")
            root = ET.fromstring(data)
            suite_props = {
                p.get("name"): p.get("value") for p in root.findall("properties/property")
            }
            self.assertEqual(
                suite_props,
                {"retry_of": '"r2"', "root_run_id": '"r1"', "attempt": "2"},
            )

    def test_open_and_missing_runs_fail_with_json_errors(self) -> None:
        with ServerHarness() as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c1"]})

            status, content_type, data = server.request(
                "GET", "/v1/runs/r1/junit.xml"
            )
            self.assertEqual(status, 409)
            self.assertEqual(content_type, "application/json; charset=utf-8")
            body = json.loads(data)
            self.assertEqual(
                body, {"error": {"code": "run_incomplete", "message": body["error"]["message"]}}
            )
            self.assertEqual(body["error"]["code"], "run_incomplete")

            status, content_type, data = server.request(
                "GET", "/v1/runs/ghost/junit.xml"
            )
            self.assertEqual(status, 404)
            self.assertEqual(content_type, "application/json; charset=utf-8")
            body = json.loads(data)
            self.assertEqual(body["error"]["code"], "run_not_found")

            # Unknown routes still use the generic not_found error.
            status, _, data = server.request("GET", "/v1/runs/r1/other.xml")
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(data)["error"]["code"], "not_found")

    def test_export_does_not_mutate_run_state(self) -> None:
        service = Service()
        with ServerHarness(service) as server:
            seed_cases(server)
            server.json("POST", "/v1/runs", {"id": "r1", "case_ids": ["c2", "c1"]})
            finish(
                server,
                "r1",
                {"c2[0]": ("passed", 1), "c2[1]": ("failed", 2), "c1[0]": ("skipped", 0)},
            )
            _, before = server.json("GET", "/v1/runs/r1")
            server.request("GET", "/v1/runs/r1/junit.xml")
            server.request("GET", "/v1/runs/r1/junit.xml")
            _, after = server.json("GET", "/v1/runs/r1")
            self.assertEqual(after, before)
            # Retry data must still be derivable exactly as before export.
            status, retry = server.json("POST", "/v1/runs/r1/retry", {"id": "r2"})
            self.assertEqual(status, 201)
            self.assertEqual(
                [i["instance_id"] for i in retry["instances"]], ["c2[1]"]
            )


if __name__ == "__main__":
    unittest.main()
