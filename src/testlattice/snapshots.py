"""Deterministic JSON snapshot comparison for POST /v1/snapshots/{id}/compare.

The diff is pure: it never reads or mutates catalog state. Differences are
emitted in a stable order (object keys by Unicode code point, array indices
ascending) with RFC 6901 pointer paths.
"""

from __future__ import annotations

import copy
from typing import Any

from .service import ApiError, _validation

_ALLOWED_FIELDS = {"actual", "ignore_paths"}
_MISSING = object()


# -- request validation --------------------------------------------------


def _validate_pointer_syntax(pointer: str) -> None:
    """Validate an RFC 6901 JSON Pointer reference syntactically."""
    if pointer and not pointer.startswith("/"):
        raise _validation(
            "ignore_paths entries must be valid RFC 6901 JSON Pointers"
        )
    for token in pointer.split("/")[1:]:
        # Unescape order matters: ~1 before ~0; a stray "~" not followed by
        # 0/1 (or ending the token) makes the reference token invalid.
        cursor = 0
        while cursor < len(token):
            if token[cursor] == "~":
                if cursor + 1 >= len(token) or token[cursor + 1] not in "01":
                    raise _validation(
                        "ignore_paths entries must be valid RFC 6901 JSON Pointers"
                    )
                cursor += 2
            else:
                cursor += 1


def validate_compare_request(payload: Any) -> tuple[Any, frozenset[str]]:
    """Validate a compare request; return (actual, ignored pointer set)."""
    if not isinstance(payload, dict):
        raise _validation("request body must be a JSON object")
    unknown = set(payload) - _ALLOWED_FIELDS
    if unknown:
        raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
    if "actual" not in payload:
        raise _validation("actual is required")

    ignored: set[str] = set()
    if "ignore_paths" in payload:
        raw = payload["ignore_paths"]
        if not isinstance(raw, list):
            raise _validation("ignore_paths must be an array")
        for index, pointer in enumerate(raw):
            if not isinstance(pointer, str):
                raise _validation(f"ignore_paths[{index}] must be a string")
            _validate_pointer_syntax(pointer)
            if pointer in ignored:
                raise _validation("ignore_paths must not contain duplicates")
            ignored.add(pointer)

    return copy.deepcopy(payload["actual"]), frozenset(ignored)


# -- diffing -------------------------------------------------------------


def _escape_token(token: str) -> str:
    """Escape an object key for an RFC 6901 reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def _values_equal(left: Any, right: Any) -> bool:
    """Scalar/type equality: booleans are not numbers; otherwise JSON equality."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    return left == right


def _diff(
    expected: Any,
    actual: Any,
    pointer: str,
    ignored: frozenset[str],
    differences: list[dict],
) -> None:
    # A hit ignores the node itself and everything below it (the traversal
    # simply never descends, so descendants need no explicit entries).
    if pointer in ignored:
        return

    if isinstance(expected, dict) and isinstance(actual, dict):
        # Union traversal in code-point order interleaves common, missing and
        # unexpected members deterministically.
        for key in sorted(set(expected) | set(actual)):
            child = f"{pointer}/{_escape_token(key)}"
            if child in ignored:
                continue
            if key not in actual:
                differences.append(
                    {
                        "path": child,
                        "code": "missing_actual",
                        "expected": copy.deepcopy(expected[key]),
                    }
                )
            elif key not in expected:
                differences.append(
                    {
                        "path": child,
                        "code": "unexpected_actual",
                        "actual": copy.deepcopy(actual[key]),
                    }
                )
            else:
                _diff(expected[key], actual[key], child, ignored, differences)
        return

    if isinstance(expected, list) and isinstance(actual, list):
        for index in range(max(len(expected), len(actual))):
            child = f"{pointer}/{index}"
            if child in ignored:
                continue
            if index >= len(actual):
                differences.append(
                    {
                        "path": child,
                        "code": "missing_actual",
                        "expected": copy.deepcopy(expected[index]),
                    }
                )
            elif index >= len(expected):
                differences.append(
                    {
                        "path": child,
                        "code": "unexpected_actual",
                        "actual": copy.deepcopy(actual[index]),
                    }
                )
            else:
                _diff(
                    expected[index], actual[index], child, ignored, differences
                )
        return

    # Containers of different kinds and unequal scalars differ only here.
    if not _values_equal(expected, actual):
        differences.append(
            {
                "path": pointer,
                "code": "value_mismatch",
                "expected": copy.deepcopy(expected),
                "actual": copy.deepcopy(actual),
            }
        )


def compare_values(
    expected: Any, actual: Any, ignored: frozenset[str] = frozenset()
) -> dict:
    """Compare ``actual`` against a stored ``expected`` value."""
    differences: list[dict] = []
    _diff(expected, actual, "", ignored, differences)
    summary = {"total": len(differences)}
    for code in ("value_mismatch", "missing_actual", "unexpected_actual"):
        summary[code] = sum(1 for item in differences if item["code"] == code)
    return {
        "passed": not differences,
        "summary": summary,
        "differences": differences,
    }
