"""Deterministic JUnit XML rendering for completed runs.

The output is a pure function of the frozen run report: no timestamps, no
wall-clock durations and no host environment are consulted, so exporting the
same unchanged run twice yields byte-identical bytes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(value: str) -> str:
    return _escape_text(value).replace('"', "&quot;")


def _format_time(duration_ms: object) -> str:
    """Format integer milliseconds as seconds with exactly three decimals.

    Integer division/modulo avoids binary-float rounding so the suite total is
    always exactly the sum of the per-testcase values.
    """
    seconds, millis = divmod(duration_ms, 1000)
    return f"{seconds}.{millis:03d}"


def _compact_json(value: object) -> str:
    # sort_keys recurses into nested objects; separators remove all incidental
    # whitespace and ensure_ascii keeps non-ASCII characters readable.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_junit_report(report: Mapping[str, object]) -> str:
    instances = report["instances"]
    summary = report["summary"]
    total_ms = sum(instance["duration_ms"] for instance in instances)

    lines = [XML_DECLARATION]
    lines.append(
        "<testsuite "
        f'name="{_escape_attr("testlattice." + report["id"])}" '
        f'tests="{summary["total"]}" '
        f'failures="{summary["failed"]}" '
        f'errors="{summary["error"]}" '
        f'skipped="{summary["skipped"]}" '
        f'time="{_format_time(total_ms)}">'
    )

    if "retry_of" in report:
        lines.append("  <properties>")
        for name in ("retry_of", "root_run_id", "attempt"):
            lines.append(
                f'    <property name="{name}" '
                f'value="{_escape_attr(_compact_json(report[name]))}"/>'
            )
        lines.append("  </properties>")

    for instance in instances:
        lines.append(
            "  <testcase "
            f'classname="{_escape_attr(instance["case_id"])}" '
            f'name="{_escape_attr(instance["instance_id"])}" '
            f'time="{_format_time(instance["duration_ms"])}">'
        )
        lines.append("    <properties>")
        lines.append(
            '      <property name="case_name" '
            f'value="{_escape_attr(_compact_json(instance["case_name"]))}"/>'
        )
        # Python's default string ordering is lexicographic by Unicode code
        # point, which is exactly the required parameter ordering.
        for name in sorted(instance["parameters"]):
            value = _compact_json(instance["parameters"][name])
            lines.append(
                f'      <property name="parameter.{_escape_attr(name)}" '
                f'value="{_escape_attr(value)}"/>'
            )
        lines.append("    </properties>")

        outcome = instance["outcome"]
        if outcome in ("failed", "error"):
            tag = "failure" if outcome == "failed" else "error"
            text = (
                _compact_json(instance["details"]) if "details" in instance else ""
            )
            lines.append(
                f'    <{tag} message="{outcome}">'
                f"{_escape_text(text)}</{tag}>"
            )
        elif outcome == "skipped":
            text = (
                _compact_json(instance["details"]) if "details" in instance else ""
            )
            lines.append(f"    <skipped>{_escape_text(text)}</skipped>")
        lines.append("  </testcase>")

    lines.append("</testsuite>")
    return "\n".join(lines)
