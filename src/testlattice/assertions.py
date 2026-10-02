"""Stateless JSON assertion evaluation.

The entry point is :func:`evaluate_assertions`, backing
``POST /v1/assertions/evaluate``. It never touches catalog state: the request
payload is validated as a whole (any failure rejects the whole request with
``validation_error``), then every assertion is evaluated in request order so a
single failure never aborts the remaining assertions.
"""

from __future__ import annotations

import copy
import re

from .service import ApiError

MAX_ASSERTIONS = 1000
OPERATORS = ("equals", "not_equals", "contains", "type")
TYPE_NAMES = ("null", "boolean", "number", "string", "array", "object")
_ARRAY_INDEX_RE = re.compile(r"(?:0|[1-9][0-9]*)")


def _validation(message: str) -> ApiError:
    return ApiError(400, "validation_error", message)


# -- JSON Pointer (RFC 6901) ----------------------------------------------


def _parse_pointer(pointer: object, index: int) -> list[str]:
    """Parse a pointer into unescaped reference tokens.

    Raises validation_error for syntax errors (missing leading slash or a
    ``~`` not followed by ``0``/``1``). Semantic resolution failures (missing
    members, invalid array indices) are left to evaluation time.
    """
    if not isinstance(pointer, str):
        raise _validation(f"assertions[{index}].path must be a string")
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise _validation(
            f"assertions[{index}].path must be a valid JSON Pointer"
        )
    tokens: list[str] = []
    for raw in pointer[1:].split("/"):
        token: list[str] = []
        pos = 0
        while pos < len(raw):
            char = raw[pos]
            if char == "~":
                if pos + 1 >= len(raw) or raw[pos + 1] not in "01":
                    raise _validation(
                        f"assertions[{index}].path must be a valid JSON Pointer"
                    )
                token.append("~" if raw[pos + 1] == "0" else "/")
                pos += 2
            else:
                token.append(char)
                pos += 1
        tokens.append("".join(token))
    return tokens


def _resolve(root: object, tokens: list[str]) -> tuple[bool, object]:
    """Walk pre-parsed tokens; returns (found, value)."""
    current = root
    for token in tokens:
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            # RFC 6901: "0" alone, digits without leading zeros, and "-"
            # always fails against an array. Out-of-range indices miss too.
            if not _ARRAY_INDEX_RE.fullmatch(token):
                return False, None
            if int(token) >= len(current):
                return False, None
            current = current[int(token)]
        else:
            return False, None
    return True, current


# -- Deep equality ----------------------------------------------------------


def _deep_equal(actual: object, expected: object) -> bool:
    """JSON structural equality.

    Booleans are not numbers, array order matters, and object member order is
    ignored.
    """
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if isinstance(actual, (int, float)) or isinstance(expected, (int, float)):
        return (
            isinstance(actual, (int, float))
            and isinstance(expected, (int, float))
            and actual == expected
        )
    if isinstance(actual, str) or isinstance(expected, str):
        return isinstance(actual, str) and isinstance(expected, str) and actual == expected
    if isinstance(actual, list) or isinstance(expected, list):
        if not isinstance(actual, list) or not isinstance(expected, list):
            return False
        if len(actual) != len(expected):
            return False
        return all(_deep_equal(a, b) for a, b in zip(actual, expected))
    if isinstance(actual, dict) or isinstance(expected, dict):
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            return False
        if actual.keys() != expected.keys():
            return False
        return all(_deep_equal(actual[key], expected[key]) for key in actual)
    return False


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _evaluate_one(operator: str, actual: object, expected: object) -> tuple[str, bool]:
    if operator == "equals":
        matched = _deep_equal(actual, expected)
        return ("ok", True) if matched else ("value_mismatch", False)
    if operator == "not_equals":
        matched = _deep_equal(actual, expected)
        return ("value_mismatch", False) if matched else ("ok", True)
    if operator == "contains":
        if isinstance(actual, str):
            if not isinstance(expected, str):
                return "type_mismatch", False
            if expected in actual:
                return "ok", True
            return "value_mismatch", False
        if isinstance(actual, list):
            if any(_deep_equal(item, expected) for item in actual):
                return "ok", True
            return "value_mismatch", False
        return "type_mismatch", False
    # operator == "type": expected is guaranteed to be a valid type name.
    if _type_name(actual) == expected:
        return "ok", True
    return "type_mismatch", False


# -- Request validation and aggregation -------------------------------------


def _validate_assertion(item: object, index: int, seen_ids: set[str]) -> dict:
    if not isinstance(item, dict):
        raise _validation(f"assertions[{index}] must be a JSON object")
    allowed = {"id", "operator", "expected", "path"}
    unknown = set(item) - allowed
    if unknown:
        raise _validation(
            f"assertions[{index}] unknown fields: {', '.join(sorted(unknown))}"
        )
    for field in ("id", "operator", "expected"):
        if field not in item:
            raise _validation(f"assertions[{index}].{field} is required")

    assertion_id = item["id"]
    if not isinstance(assertion_id, str) or assertion_id == "":
        raise _validation(f"assertions[{index}].id must be a non-empty string")
    if assertion_id in seen_ids:
        raise _validation(f"assertions[{index}].id {assertion_id!r} is duplicated")
    seen_ids.add(assertion_id)

    operator = item["operator"]
    if not isinstance(operator, str) or operator not in OPERATORS:
        raise _validation(
            f"assertions[{index}].operator must be one of: {', '.join(OPERATORS)}"
        )

    expected = item["expected"]
    if operator == "type" and (not isinstance(expected, str) or expected not in TYPE_NAMES):
        raise _validation(
            f"assertions[{index}].expected must be one of: {', '.join(TYPE_NAMES)}"
        )

    path = item["path"] if "path" in item else ""
    tokens = _parse_pointer(path, index)

    return {
        "id": assertion_id,
        "operator": operator,
        "expected": expected,
        "path": path,
        "tokens": tokens,
    }


def evaluate_assertions(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise _validation("request body must be a JSON object")
    unknown = set(payload) - {"actual", "assertions"}
    if unknown:
        raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
    if "actual" not in payload:
        raise _validation("actual is required")
    if "assertions" not in payload:
        raise _validation("assertions is required")

    raw_assertions = payload["assertions"]
    if not isinstance(raw_assertions, list):
        raise _validation("assertions must be an array")
    if not 1 <= len(raw_assertions) <= MAX_ASSERTIONS:
        raise _validation(
            f"assertions must contain between 1 and {MAX_ASSERTIONS} items"
        )

    seen_ids: set[str] = set()
    assertions = [
        _validate_assertion(item, index, seen_ids)
        for index, item in enumerate(raw_assertions)
    ]

    actual_root = payload["actual"]
    results: list[dict] = []
    passed_count = 0
    for assertion in assertions:
        found, value = _resolve(actual_root, assertion["tokens"])
        result: dict = {
            "id": assertion["id"],
            "path": assertion["path"],
            "operator": assertion["operator"],
            "expected": copy.deepcopy(assertion["expected"]),
        }
        if not found:
            result["passed"] = False
            result["code"] = "path_not_found"
        else:
            result["actual"] = copy.deepcopy(value)
            code, passed = _evaluate_one(
                assertion["operator"], value, assertion["expected"]
            )
            result["passed"] = passed
            result["code"] = code
            if passed:
                passed_count += 1
        results.append(result)

    total = len(results)
    failed_count = total - passed_count
    return {
        "passed": failed_count == 0,
        "summary": {
            "total": total,
            "passed": passed_count,
            "failed": failed_count,
        },
        "results": results,
    }
