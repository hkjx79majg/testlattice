"""Deterministic JSON snapshot comparison for /v1/snapshots routes.

The diff engine walks the stored (expected) and actual JSON values in a
fixed order — object keys by Unicode code point, array indices ascending —
and records every difference with an RFC 6901 escaped path. Comparison is
pure: it never mutates either value.
"""

from __future__ import annotations

import copy
from typing import Any

from .service import ApiError, _validation

MISSING_ACTUAL = "missing_actual"
UNEXPECTED_ACTUAL = "unexpected_actual"
VALUE_MISMATCH = "value_mismatch"
DIFF_CODES = (MISSING_ACTUAL, UNEXPECTED_ACTUAL, VALUE_MISMATCH)


def _escape_token(token: str) -> str:
    """RFC 6901 escaping for a single reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def _validate_pointer_syntax(pointer: str, index: int) -> None:
    if pointer and not pointer.startswith("/"):
        raise _validation(
            f"ignore_paths[{index}] must be a valid RFC 6901 JSON Pointer"
        )
    for token in pointer.split("/")[1:]:
        # Unescape order matters: ~1 before ~0; a stray "~" not followed by 0/1
        # (or ending the token) makes the reference token invalid.
        cursor = 0
        while cursor < len(token):
            if token[cursor] == "~":
                if cursor + 1 >= len(token) or token[cursor + 1] not in "01":
                    raise _validation(
                        f"ignore_paths[{index}] must be a valid RFC 6901 JSON Pointer"
                    )
                cursor += 2
            else:
                cursor += 1


def validate_ignore_paths(value: Any) -> list[str]:
    """Validate the optional ignore_paths array: unique RFC 6901 pointers."""
    if not isinstance(value, list):
        raise _validation("ignore_paths must be an array")
    cleaned: list[str] = []
    for index, pointer in enumerate(value):
        if not isinstance(pointer, str):
            raise _validation(f"ignore_paths[{index}] must be a string")
        _validate_pointer_syntax(pointer, index)
        if pointer in cleaned:
            raise _validation("ignore_paths must not contain duplicates")
        cleaned.append(pointer)
    return cleaned


def validate_compare_payload(payload: Any) -> tuple[Any, list[str]]:
    """Validate a compare request body; return (actual, ignore_paths)."""
    if not isinstance(payload, dict):
        raise _validation("request body must be a JSON object")
    unknown = set(payload) - {"actual", "ignore_paths"}
    if unknown:
        raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
    if "actual" not in payload:
        raise _validation("actual is required")
    ignore_paths: list[str] = []
    if "ignore_paths" in payload and payload["ignore_paths"] is not None:
        ignore_paths = validate_ignore_paths(payload["ignore_paths"])
    return payload["actual"], ignore_paths


def _is_container(value: Any) -> bool:
    return isinstance(value, (dict, list))


def _scalar_equal(left: Any, right: Any) -> bool:
    """JSON scalar equality: booleans are never equal to numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    return bool(left == right)


def _diff(expected: Any, actual: Any, path: str, ignored: set[str], out: list[dict]) -> None:
    if path in ignored:
        return
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child = path + "/" + _escape_token(key)
            if key not in actual:
                if child not in ignored:
                    out.append(
                        {
                            "path": child,
                            "code": MISSING_ACTUAL,
                            "expected": copy.deepcopy(expected[key]),
                        }
                    )
            elif key not in expected:
                if child not in ignored:
                    out.append(
                        {
                            "path": child,
                            "code": UNEXPECTED_ACTUAL,
                            "actual": copy.deepcopy(actual[key]),
                        }
                    )
            else:
                _diff(expected[key], actual[key], child, ignored, out)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        shared = min(len(expected), len(actual))
        for index in range(shared):
            _diff(expected[index], actual[index], f"{path}/{index}", ignored, out)
        for index in range(shared, len(expected)):
            child = f"{path}/{index}"
            if child not in ignored:
                out.append(
                    {
                        "path": child,
                        "code": MISSING_ACTUAL,
                        "expected": copy.deepcopy(expected[index]),
                    }
                )
        for index in range(shared, len(actual)):
            child = f"{path}/{index}"
            if child not in ignored:
                out.append(
                    {
                        "path": child,
                        "code": UNEXPECTED_ACTUAL,
                        "actual": copy.deepcopy(actual[index]),
                    }
                )
        return
    # Container kind mismatch or unequal scalars: one difference right here.
    if _is_container(expected) or _is_container(actual) or not _scalar_equal(expected, actual):
        out.append(
            {
                "path": path,
                "code": VALUE_MISMATCH,
                "expected": copy.deepcopy(expected),
                "actual": copy.deepcopy(actual),
            }
        )


def diff_values(expected: Any, actual: Any, ignore_paths: list[str]) -> dict:
    """Compare two JSON values; return passed/summary/differences."""
    ignored = set(ignore_paths)
    differences: list[dict] = []
    _diff(expected, actual, "", ignored, differences)
    counts = {code: 0 for code in DIFF_CODES}
    for difference in differences:
        counts[difference["code"]] += 1
    return {
        "passed": not differences,
        "summary": {"total": len(differences), **counts},
        "differences": differences,
    }
