"""Deterministic JUnit XML rendering for completed runs.

The output is a hand-rendered string (rather than ``xml.etree``) so that the
XML declaration, attribute order, compact JSON values and empty elements stay
byte-for-byte stable for an unchanged run.
"""

from __future__ import annotations

import json
from xml.sax.saxutils import escape

_RESULT_TAGS = (
    ("failed", "failure", "failed"),
    ("error", "error", "error"),
    ("skipped", "skipped", None),
)


def _json(value: object) -> str:
    """Compact JSON: no surplus whitespace, non-ASCII left unescaped,
    object keys recursively ordered by Unicode code point."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _attr_text(value: str) -> str:
    return escape(value, entities={'"': "&quot;"})


def _body_text(value: str) -> str:
    return escape(value)


def _seconds(duration_ms: int) -> str:
    """Milliseconds to seconds with exactly three decimal places, formatted
    with integer arithmetic to avoid any float rounding ambiguity."""
    return f"{duration_ms // 1000}.{duration_ms % 1000:03d}"


def _property_line(indent: str, name: str, value: object) -> str:
    encoded = _attr_text(_json(value))
    return f'{indent}<property name="{_attr_text(name)}" value="{encoded}"/>'


def render_junit_xml(report: dict) -> str:
    summary = report["summary"]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        '<testsuite '
        f'name="{_attr_text("testlattice." + report["id"])}" '
        f'tests="{summary["total"]}" '
        f'failures="{summary["failed"]}" '
        f'errors="{summary["error"]}" '
        f'skipped="{summary["skipped"]}" '
        f'time="{_seconds(summary["duration_ms"])}">'
    )

    if "retry_of" in report:
        lines.append("  <properties>")
        lines.append(_property_line("    ", "retry_of", report["retry_of"]))
        lines.append(_property_line("    ", "root_run_id", report["root_run_id"]))
        lines.append(_property_line("    ", "attempt", report["attempt"]))
        lines.append("  </properties>")

    for instance in report["instances"]:
        lines.append(
            "  <testcase "
            f'classname="{_attr_text(instance["case_id"])}" '
            f'name="{_attr_text(instance["instance_id"])}" '
            f'time="{_seconds(instance["duration_ms"])}">'
        )
        lines.append("    <properties>")
        lines.append(_property_line("      ", "case_name", instance["case_name"]))
        for name in sorted(instance["parameters"]):
            lines.append(
                _property_line(
                    "      ", "parameter." + name, instance["parameters"][name]
                )
            )
        lines.append("    </properties>")

        outcome = instance["outcome"]
        for result_outcome, tag, message in _RESULT_TAGS:
            if outcome == result_outcome:
                text = _json(instance["details"]) if "details" in instance else ""
                if message is None:
                    lines.append(f"    <{tag}>{_body_text(text)}</{tag}>")
                else:
                    lines.append(
                        f'    <{tag} message="{message}">{_body_text(text)}</{tag}>'
                    )
                break
        lines.append("  </testcase>")

    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"
