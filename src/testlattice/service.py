"""Core service surface for TestLattice.

The catalog keeps suites and cases in memory; all state is lost when the
process restarts. Access is guarded by a lock because the HTTP layer runs
on a threading server.
"""

from __future__ import annotations

import copy
import math
import re
import threading
from collections.abc import Mapping

from . import __version__

ALLOWED_KINDS = ("unit", "api", "browser", "contract")
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 86400
MAX_INSTANCES = 1000
_PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ApiError(Exception):
    """An error that maps directly onto an HTTP error response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _validation(message: str) -> ApiError:
    return ApiError(400, "validation_error", message)


def _clean_text(value: object, field: str) -> str:
    """Validate id/name: string that is non-empty after trimming."""
    if not isinstance(value, str):
        raise _validation(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise _validation(f"{field} must not be empty")
    return cleaned


def _reference(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _validation(f"{field} must be a non-empty string")
    return value


def _is_scalar(value: object) -> bool:
    """Parameter values may be strings, numbers, booleans or null."""
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    return False


def _valid_param_name(name: object) -> bool:
    return isinstance(name, str) and _PARAM_NAME_RE.fullmatch(name) is not None


def _validate_parameterization(value: object) -> dict:
    """Validate the optional parameterization block; exactly one of axes/rows."""
    if not isinstance(value, dict):
        raise _validation("parameterization must be a JSON object")
    unknown = set(value) - {"axes", "rows"}
    if unknown:
        raise _validation(
            f"parameterization unknown fields: {', '.join(sorted(unknown))}"
        )
    if "axes" in value and "rows" in value:
        raise _validation("parameterization must use either axes or rows, not both")
    if "axes" in value:
        return {"axes": _validate_axes(value["axes"])}
    if "rows" in value:
        return {"rows": _validate_rows(value["rows"])}
    raise _validation("parameterization must contain either axes or rows")


def _validate_axes(axes: object) -> dict:
    if not isinstance(axes, dict) or not axes:
        raise _validation("parameterization.axes must be a non-empty object")
    cleaned: dict[str, list] = {}
    for name, values in axes.items():
        if not _valid_param_name(name):
            raise _validation(
                f"parameterization axis name {name!r} must match [A-Za-z_][A-Za-z0-9_]*"
            )
        if not isinstance(values, list) or not values:
            raise _validation(f"parameterization.axes.{name} must be a non-empty array")
        for index, item in enumerate(values):
            if not _is_scalar(item):
                raise _validation(
                    f"parameterization.axes.{name}[{index}] must be a scalar"
                )
        cleaned[name] = copy.deepcopy(values)
    count = 1
    for values in cleaned.values():
        count *= len(values)
    if count > MAX_INSTANCES:
        raise _validation(
            f"parameterization expands to {count} instances; limit is {MAX_INSTANCES}"
        )
    return cleaned


def _validate_rows(rows: object) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise _validation("parameterization.rows must be a non-empty array")
    cleaned: list[dict] = []
    expected: tuple[str, ...] | None = None
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict) or not row:
            raise _validation(
                f"parameterization.rows[{row_index}] must be a non-empty object"
            )
        names = tuple(row)
        for name in names:
            if not _valid_param_name(name):
                raise _validation(
                    f"parameterization rows[{row_index}] parameter name {name!r} "
                    "must match [A-Za-z_][A-Za-z0-9_]*"
                )
        if expected is None:
            expected = names
        elif set(names) != set(expected):
            raise _validation(
                "parameterization.rows must all have the same set of parameter names"
            )
        for name in expected:
            item = row[name]
            if not _is_scalar(item):
                raise _validation(
                    f"parameterization.rows[{row_index}].{name} must be a scalar"
                )
        # Preserve the canonical parameter order taken from the first row.
        cleaned.append({name: copy.deepcopy(row[name]) for name in expected})
    if len(cleaned) > MAX_INSTANCES:
        raise _validation(
            f"parameterization expands to {len(cleaned)} instances; limit is {MAX_INSTANCES}"
        )
    return cleaned


def _expand_parameterization(parameterization: dict | None) -> list[dict]:
    """Expand a validated definition into ordered parameter maps."""
    if parameterization is None:
        return [{}]
    if "axes" in parameterization:
        axes = parameterization["axes"]
        combinations: list[dict] = [{}]
        # Axes keep request declaration order; the rightmost axis varies fastest,
        # so extend the running product one full axis at a time.
        for name, values in axes.items():
            combinations = [
                {**combo, name: value} for combo in combinations for value in values
            ]
        return combinations
    return copy.deepcopy(parameterization["rows"])


class Service:
    """In-memory catalog plus process health reporting."""

    name = "testlattice"
    version = __version__

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._suites: dict[str, dict] = {}
        self._cases: dict[str, dict] = {}

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    # -- suites ---------------------------------------------------------

    def create_suite(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"id", "name", "parent_id"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        if "id" not in payload:
            raise _validation("id is required")
        if "name" not in payload:
            raise _validation("name is required")

        suite_id = _clean_text(payload["id"], "id")
        name = _clean_text(payload["name"], "name")
        parent_id = None
        if "parent_id" in payload and payload["parent_id"] is not None:
            parent_id = _reference(payload["parent_id"], "parent_id")

        with self._lock:
            if suite_id in self._suites:
                raise ApiError(409, "suite_exists", f"suite {suite_id!r} already exists")
            if parent_id is not None:
                if parent_id == suite_id:
                    raise _validation("suite must not reference itself as parent")
                if parent_id not in self._suites:
                    raise ApiError(404, "suite_not_found", f"suite {parent_id!r} not found")
                # Walk the ancestor chain; reaching the new id would be a cycle.
                ancestor = self._suites[parent_id].get("parent_id")
                seen = {parent_id}
                while ancestor is not None:
                    if ancestor == suite_id:
                        raise _validation("suite hierarchy must not contain a cycle")
                    if ancestor in seen or ancestor not in self._suites:
                        break
                    seen.add(ancestor)
                    ancestor = self._suites[ancestor].get("parent_id")

            suite = {"id": suite_id, "name": name, "parent_id": parent_id}
            self._suites[suite_id] = suite
            return copy.deepcopy(suite)

    def get_suite(self, suite_id: str) -> dict:
        with self._lock:
            suite = self._suites.get(suite_id)
            if suite is None:
                raise ApiError(404, "suite_not_found", f"suite {suite_id!r} not found")
            return copy.deepcopy(suite)

    def list_suites(self) -> list[dict]:
        """Depth-first ordering: parents before children, creation order per level."""
        with self._lock:
            children: dict[str, list[str]] = {}
            for suite in self._suites.values():
                children.setdefault(suite["parent_id"], []).append(suite["id"])

            result: list[dict] = []

            def emit(suite_id: str) -> None:
                result.append(copy.deepcopy(self._suites[suite_id]))
                for child_id in children.get(suite_id, ()):
                    emit(child_id)

            for root_id in children.get(None, ()):
                emit(root_id)
            # Defensive: suites whose parent vanished still surface deterministically.
            rooted = {s["id"] for s in result}
            for suite_id in self._suites:
                if suite_id not in rooted:
                    emit(suite_id)
            return result

    def delete_suite(self, suite_id: str) -> None:
        with self._lock:
            if suite_id not in self._suites:
                raise ApiError(404, "suite_not_found", f"suite {suite_id!r} not found")

            subtree = {suite_id}
            frontier = [suite_id]
            while frontier:
                current = frontier.pop()
                for other in self._suites.values():
                    if other["parent_id"] == current and other["id"] not in subtree:
                        subtree.add(other["id"])
                        frontier.append(other["id"])

            for suite in self._suites.values():
                if suite["id"] in subtree and suite["id"] != suite_id:
                    raise ApiError(409, "suite_not_empty", f"suite {suite_id!r} is not empty")
            for case in self._cases.values():
                if case["suite_id"] in subtree:
                    raise ApiError(409, "suite_not_empty", f"suite {suite_id!r} is not empty")

            del self._suites[suite_id]

    # -- cases ----------------------------------------------------------

    def _validate_case(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        allowed = {
            "id",
            "name",
            "suite_id",
            "kind",
            "steps",
            "tags",
            "enabled",
            "timeout_seconds",
            "parameterization",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        for field in ("id", "name", "suite_id", "kind", "steps"):
            if field not in payload:
                raise _validation(f"{field} is required")

        case_id = _clean_text(payload["id"], "id")
        name = _clean_text(payload["name"], "name")
        suite_id = _reference(payload["suite_id"], "suite_id")

        kind = payload["kind"]
        if not isinstance(kind, str) or kind not in ALLOWED_KINDS:
            raise _validation(f"kind must be one of: {', '.join(ALLOWED_KINDS)}")

        steps = payload["steps"]
        if not isinstance(steps, list) or not steps:
            raise _validation("steps must be a non-empty array")
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise _validation(f"steps[{index}] must be a JSON object")
            action = step.get("action")
            if not isinstance(action, str) or action == "":
                raise _validation(f"steps[{index}].action must be a non-empty string")

        tags: list[str] = []
        if "tags" in payload and payload["tags"] is not None:
            raw_tags = payload["tags"]
            if not isinstance(raw_tags, list):
                raise _validation("tags must be an array")
            for index, tag in enumerate(raw_tags):
                if not isinstance(tag, str) or tag == "":
                    raise _validation(f"tags[{index}] must be a non-empty string")
                if tag not in tags:
                    tags.append(tag)

        enabled = True
        if "enabled" in payload and payload["enabled"] is not None:
            if not isinstance(payload["enabled"], bool):
                raise _validation("enabled must be a boolean")
            enabled = payload["enabled"]

        timeout = DEFAULT_TIMEOUT_SECONDS
        if "timeout_seconds" in payload and payload["timeout_seconds"] is not None:
            value = payload["timeout_seconds"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise _validation("timeout_seconds must be an integer")
            if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
                raise _validation(
                    f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
                )
            timeout = value

        case = {
            "id": case_id,
            "name": name,
            "suite_id": suite_id,
            "kind": kind,
            "steps": copy.deepcopy(steps),
            "tags": tags,
            "enabled": enabled,
            "timeout_seconds": timeout,
        }
        if "parameterization" in payload and payload["parameterization"] is not None:
            case["parameterization"] = _validate_parameterization(
                payload["parameterization"]
            )
        return case

    def create_case(self, payload: object) -> dict:
        case = self._validate_case(payload)
        with self._lock:
            if case["id"] in self._cases:
                raise ApiError(409, "case_exists", f"case {case['id']!r} already exists")
            if case["suite_id"] not in self._suites:
                raise ApiError(404, "suite_not_found", f"suite {case['suite_id']!r} not found")
            self._cases[case["id"]] = case
            return copy.deepcopy(case)

    def get_case(self, case_id: str) -> dict:
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise ApiError(404, "case_not_found", f"case {case_id!r} not found")
            return copy.deepcopy(case)

    def get_instances(self, case_id: str) -> dict:
        """Preview the execution instances without mutating catalog state."""
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise ApiError(404, "case_not_found", f"case {case_id!r} not found")
            parameterization = case.get("parameterization")
            parameters = _expand_parameterization(parameterization)
            instances = [
                {"id": f"{case_id}[{index}]", "parameters": copy.deepcopy(values)}
                for index, values in enumerate(parameters)
            ]
            return {"case_id": case_id, "count": len(instances), "instances": instances}

    def list_cases(self, filters: Mapping[str, object]) -> list[dict]:
        suite_id = filters.get("suite_id")
        include_descendants = bool(filters.get("include_descendants", False))
        kind = filters.get("kind")
        enabled = filters.get("enabled")
        required_tags = filters.get("tags", ())

        with self._lock:
            allowed_suites: set[str] | None = None
            if suite_id is not None:
                if suite_id not in self._suites:
                    raise ApiError(404, "suite_not_found", f"suite {suite_id!r} not found")
                allowed_suites = {suite_id}
                if include_descendants:
                    frontier = [suite_id]
                    while frontier:
                        current = frontier.pop()
                        for suite in self._suites.values():
                            if suite["parent_id"] == current and suite["id"] not in allowed_suites:
                                allowed_suites.add(suite["id"])
                                frontier.append(suite["id"])

            result: list[dict] = []
            for case in self._cases.values():
                if allowed_suites is not None and case["suite_id"] not in allowed_suites:
                    continue
                if kind is not None and case["kind"] != kind:
                    continue
                if enabled is not None and case["enabled"] != enabled:
                    continue
                if required_tags and not all(tag in case["tags"] for tag in required_tags):
                    continue
                result.append(copy.deepcopy(case))
            return result

    def delete_case(self, case_id: str) -> None:
        with self._lock:
            if case_id not in self._cases:
                raise ApiError(404, "case_not_found", f"case {case_id!r} not found")
            del self._cases[case_id]
