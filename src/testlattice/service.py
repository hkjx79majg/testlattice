"""Core service surface for TestLattice.

The catalog keeps suites and cases in memory; all state is lost when the
process restarts. Access is guarded by a lock because the HTTP layer runs
on a threading server.
"""

from __future__ import annotations

import copy
import itertools
import re
import threading
from collections.abc import Mapping
from typing import TypeGuard

from . import __version__

ALLOWED_KINDS = ("unit", "api", "browser", "contract")
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 86400
MAX_INSTANCES = 1000
_AXIS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_scalar(value: object) -> TypeGuard[str | int | float | bool | None]:
    """A permitted parameter value: string, number, boolean or null.

    bool is a subclass of int, but it is still its own scalar kind here.
    """
    return value is None or isinstance(value, (str, int, float, bool))


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


def _check_axis_name(name: object) -> str:
    if not isinstance(name, str) or not _AXIS_NAME.fullmatch(name):
        raise _validation(
            f"parameter name {name!r} must match {_AXIS_NAME.pattern}"
        )
    return name


def _check_scalar(value: object, location: str) -> object:
    if not _is_scalar(value):
        raise _validation(f"{location} must be a string, number, boolean or null")
    return value


def _validate_parameterization(raw: object) -> dict:
    """Validate the optional parameterization block and return a detached copy.

    Exactly one of ``axes``/``rows`` is allowed. The expanded instance count is
    capped at ``MAX_INSTANCES``; duplicates are intentionally preserved.
    """
    if not isinstance(raw, dict):
        raise _validation("parameterization must be a JSON object")
    unknown = set(raw) - {"axes", "rows"}
    if unknown:
        raise _validation(
            f"unknown fields in parameterization: {', '.join(sorted(unknown))}"
        )
    if "axes" in raw and "rows" in raw:
        raise _validation("parameterization must use either axes or rows, not both")
    if "axes" not in raw and "rows" not in raw:
        raise _validation("parameterization must contain axes or rows")

    if "axes" in raw:
        axes = raw["axes"]
        if not isinstance(axes, dict) or not axes:
            raise _validation("parameterization.axes must be a non-empty object")
        normalized_axes: dict[str, list] = {}
        count = 1
        for name, values in axes.items():
            _check_axis_name(name)
            if not isinstance(values, list) or not values:
                raise _validation(f"parameterization.axes.{name} must be a non-empty array")
            normalized: list = []
            for index, value in enumerate(values):
                normalized.append(_check_scalar(value, f"parameterization.axes.{name}[{index}]"))
            normalized_axes[name] = normalized
            count *= len(normalized)
            if count > MAX_INSTANCES:
                raise _validation(
                    f"parameterization expands to more than {MAX_INSTANCES} instances"
                )
        return {"axes": normalized_axes}

    rows = raw["rows"]
    if not isinstance(rows, list) or not rows:
        raise _validation("parameterization.rows must be a non-empty array")
    names: list[str] | None = None
    normalized_rows: list[dict[str, object]] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict) or not row:
            raise _validation(f"parameterization.rows[{row_index}] must be a non-empty object")
        row_names: list[str] = []
        for name in row:
            _check_axis_name(name)
            if name not in row_names:
                row_names.append(name)
        if names is None:
            names = row_names
        elif set(row_names) != set(names):
            raise _validation(
                "parameterization.rows must all have the same set of parameter names"
            )
        normalized_row = {
            name: _check_scalar(row[name], f"parameterization.rows[{row_index}].{name}")
            for name in names
        }
        normalized_rows.append(normalized_row)
    if len(normalized_rows) > MAX_INSTANCES:
        raise _validation(
            f"parameterization expands to more than {MAX_INSTANCES} instances"
        )
    return {"rows": normalized_rows}


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

        parameterization: dict | None = None
        if "parameterization" in payload and payload["parameterization"] is not None:
            parameterization = _validate_parameterization(payload["parameterization"])

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
        if parameterization is not None:
            case["parameterization"] = parameterization
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

    def list_instances(self, case_id: str) -> dict:
        """Expand a case's parameterization into stable, previewable instances.

        A case without parameterization yields a single instance with empty
        parameters. Expansion never mutates catalog state.
        """
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise ApiError(404, "case_not_found", f"case {case_id!r} not found")
            parameterization = case.get("parameterization")
            if parameterization is None:
                parameters_rows: list[dict[str, object]] = [{}]
            elif "axes" in parameterization:
                axes = parameterization["axes"]
                names = list(axes)
                parameters_rows = [
                    dict(zip(names, combo, strict=True))
                    for combo in itertools.product(*(axes[name] for name in names))
                ]
            else:
                parameters_rows = copy.deepcopy(parameterization["rows"])

            instances = [
                {"id": f"{case_id}[{index}]", "parameters": copy.deepcopy(parameters)}
                for index, parameters in enumerate(parameters_rows)
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
