"""Stateless JSON value assertions for POST /v1/assertions/evaluate.

The evaluator never touches catalog state: it validates the request, resolves
RFC 6901 JSON pointers against ``actual`` and evaluates each assertion in
request order, continuing after individual failures.
"""

from __future__ import annotations

import copy
from typing import Any

from .service import ApiError, _validation

MAX_ASSERTIONS = 1000
ALLOWED_OPERATORS = ("equals", "not_equals", "contains", "type")
TYPE_NAMES = ("null", "boolean", "number", "string", "array", "object")
_ALLOWED_FIELDS = {"id", "operator", "expected", "path"}
_MISSING = object()


def _resolve_pointer(root: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON pointer; return _MISSING when it does not exist."""
    if pointer == "":
        return root
    current = root
    # Every pointer other than the empty string starts with "/".
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            if not token or not all("0" <= char <= "9" for char in token):
                return _MISSING
            if token != "0" and token.startswith("0"):
                return _MISSING
            index = int(token)
            if not 0 <= index < len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _json_type_name(value: Any) -> str:
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


def _deep_equal(left: Any, right: Any) -> bool:
    """Structural JSON equality: booleans are not numbers; object order ignored."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _deep_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _deep_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _evaluate_assertion(assertion: dict, actual: Any) -> dict:
    assertion_id = assertion["id"]
    pointer = assertion["path"]
    operator = assertion["operator"]
    expected = assertion["expected"]

    resolved = _resolve_pointer(actual, pointer)
    base: dict[str, Any] = {
        "id": assertion_id,
        "path": pointer,
        "operator": operator,
        "expected": copy.deepcopy(expected),
    }
    if resolved is _MISSING:
        return {**base, "passed": False, "code": "path_not_found"}
    base["actual"] = copy.deepcopy(resolved)

    if operator == "equals":
        passed = _deep_equal(resolved, expected)
    elif operator == "not_equals":
        passed = not _deep_equal(resolved, expected)
    elif operator == "contains":
        if isinstance(resolved, str):
            if not isinstance(expected, str):
                return {**base, "passed": False, "code": "type_mismatch"}
            passed = expected in resolved
        elif isinstance(resolved, list):
            # `expected` is the element to look for; it may have any JSON type.
            passed = any(_deep_equal(item, expected) for item in resolved)
        else:
            return {**base, "passed": False, "code": "type_mismatch"}
    else:  # type
        passed = _json_type_name(resolved) == expected
        if not passed:
            return {**base, "passed": False, "code": "type_mismatch"}

    return {**base, "passed": passed, "code": "ok" if passed else "value_mismatch"}


def _validate_pointer(pointer: Any, index: int) -> str:
    if not isinstance(pointer, str):
        raise _validation(f"assertions[{index}].path must be a string")
    if pointer and not pointer.startswith("/"):
        raise _validation(
            f"assertions[{index}].path must be a valid RFC 6901 JSON Pointer"
        )
    for token in pointer.split("/")[1:]:
        # Unescape order matters: ~1 before ~0; a stray "~" not followed by 0/1
        # (or ending the token) makes the reference token invalid.
        cursor = 0
        while cursor < len(token):
            if token[cursor] == "~":
                if cursor + 1 >= len(token) or token[cursor + 1] not in "01":
                    raise _validation(
                        f"assertions[{index}].path must be a valid RFC 6901 JSON Pointer"
                    )
                cursor += 2
            else:
                cursor += 1
    return pointer


def _validate_assertions(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise _validation("assertions must be an array")
    if not value:
        raise _validation("assertions must not be empty")
    if len(value) > MAX_ASSERTIONS:
        raise _validation(
            f"assertions must contain at most {MAX_ASSERTIONS} entries"
        )

    seen_ids: set[str] = set()
    cleaned: list[dict] = []
    for index, assertion in enumerate(value):
        if not isinstance(assertion, dict):
            raise _validation(f"assertions[{index}] must be a JSON object")
        unknown = set(assertion) - _ALLOWED_FIELDS
        if unknown:
            raise _validation(
                f"assertions[{index}] unknown fields: {', '.join(sorted(unknown))}"
            )
        for field in ("id", "operator", "expected"):
            if field not in assertion:
                raise _validation(f"assertions[{index}].{field} is required")

        assertion_id = assertion["id"]
        if not isinstance(assertion_id, str) or assertion_id == "":
            raise _validation(f"assertions[{index}].id must be a non-empty string")
        if assertion_id in seen_ids:
            raise _validation(f"assertions[{index}] id {assertion_id!r} is duplicated")
        seen_ids.add(assertion_id)

        operator = assertion["operator"]
        if not isinstance(operator, str) or operator not in ALLOWED_OPERATORS:
            raise _validation(
                f"assertions[{index}].operator must be one of: "
                f"{', '.join(ALLOWED_OPERATORS)}"
            )

        expected = assertion["expected"]
        if operator == "type":
            if not isinstance(expected, str) or expected not in TYPE_NAMES:
                raise _validation(
                    f"assertions[{index}].expected must be one of: "
                    f"{', '.join(TYPE_NAMES)}"
                )

        pointer = ""
        if "path" in assertion:
            pointer = _validate_pointer(assertion["path"], index)

        cleaned.append(
            {
                "id": assertion_id,
                "operator": operator,
                "expected": copy.deepcopy(expected),
                "path": pointer,
            }
        )
    return cleaned


def evaluate_assertions(payload: Any) -> dict:
    """Validate and evaluate an assertion request without touching catalog data."""
    if not isinstance(payload, dict):
        raise _validation("request body must be a JSON object")
    unknown = set(payload) - {"actual", "assertions"}
    if unknown:
        raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
    if "actual" not in payload:
        raise _validation("actual is required")
    if "assertions" not in payload:
        raise _validation("assertions is required")

    assertions = _validate_assertions(payload["assertions"])
    actual = payload["actual"]

    results = [_evaluate_assertion(assertion, actual) for assertion in assertions]
    passed_count = sum(1 for result in results if result["passed"])
    return {
        "passed": passed_count == len(results),
        "summary": {
            "total": len(results),
            "passed": passed_count,
            "failed": len(results) - passed_count,
        },
        "results": results,
    }
