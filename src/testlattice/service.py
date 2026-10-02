"""Core service surface for TestLattice.

The frozen baseline only reports process health. This module now also hosts
the in-process catalog of test suites and test cases backing /v1/suites and
/v1/cases. All state lives in memory, so restarting the process clears it.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from . import __version__

CASE_KINDS = frozenset({"unit", "api", "browser", "contract"})
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 86400

_SUITE_FIELDS = frozenset({"id", "name", "parent_id"})
_CASE_FIELDS = frozenset(
    {"id", "name", "suite_id", "kind", "steps", "tags", "enabled", "timeout_seconds"}
)

_MISSING = object()


class ServiceError(Exception):
    """Error carrying the HTTP status and machine-readable code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _validation(message: str) -> ServiceError:
    return ServiceError(400, "validation_error", message)


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field, _MISSING)
    if value is _MISSING:
        raise _validation(f"{field} is required")
    if not isinstance(value, str):
        raise _validation(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise _validation(f"{field} must not be empty")
    return cleaned


class Service:
    """Health reporting plus the in-memory suite/case catalog."""

    name = "testlattice"
    version = __version__

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._suites: dict[str, dict[str, Any]] = {}
        self._cases: dict[str, dict[str, Any]] = {}

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    # ------------------------------------------------------------------
    # Suites
    # ------------------------------------------------------------------

    def create_suite(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = sorted(set(payload) - _SUITE_FIELDS)
        if unknown:
            raise _validation(f"unknown fields: {', '.join(unknown)}")
        suite_id = _required_string(payload, "id")
        name = _required_string(payload, "name")
        parent_id = payload.get("parent_id")
        if parent_id is not None:
            if not isinstance(parent_id, str):
                raise _validation("parent_id must be a string")
            parent_id = parent_id.strip()
            if not parent_id:
                raise _validation("parent_id must not be empty")
        with self._lock:
            if suite_id in self._suites:
                raise ServiceError(409, "suite_exists", f"suite {suite_id!r} already exists")
            if parent_id is not None:
                if parent_id == suite_id:
                    raise _validation("suite cannot be its own parent")
                if parent_id not in self._suites:
                    raise ServiceError(
                        404, "suite_not_found", f"parent suite {parent_id!r} not found"
                    )
            suite = {"id": suite_id, "name": name, "parent_id": parent_id}
            self._suites[suite_id] = suite
            return copy.deepcopy(suite)

    def get_suite(self, suite_id: str) -> dict[str, Any]:
        with self._lock:
            suite = self._suites.get(suite_id)
            if suite is None:
                raise ServiceError(404, "suite_not_found", f"suite {suite_id!r} not found")
            return copy.deepcopy(suite)

    def list_suites(self) -> list[dict[str, Any]]:
        """Depth-first, parents before children, creation order per level."""
        with self._lock:
            children: dict[str, list[str]] = {}
            roots: list[str] = []
            for suite in self._suites.values():
                parent_id = suite["parent_id"]
                if parent_id is None:
                    roots.append(suite["id"])
                else:
                    children.setdefault(parent_id, []).append(suite["id"])
            ordered: list[dict[str, Any]] = []

            def visit(suite_id: str) -> None:
                ordered.append(copy.deepcopy(self._suites[suite_id]))
                for child_id in children.get(suite_id, []):
                    visit(child_id)

            for root_id in roots:
                visit(root_id)
            return ordered

    def delete_suite(self, suite_id: str) -> None:
        with self._lock:
            if suite_id not in self._suites:
                raise ServiceError(404, "suite_not_found", f"suite {suite_id!r} not found")
            for suite in self._suites.values():
                if suite["parent_id"] == suite_id:
                    raise ServiceError(
                        409, "suite_not_empty", f"suite {suite_id!r} has child suites"
                    )
            for case in self._cases.values():
                if case["suite_id"] == suite_id:
                    raise ServiceError(
                        409, "suite_not_empty", f"suite {suite_id!r} has cases"
                    )
            del self._suites[suite_id]

    # ------------------------------------------------------------------
    # Cases
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_steps(steps: Any) -> list[Any]:
        if steps is _MISSING:
            raise _validation("steps is required")
        if not isinstance(steps, list) or not steps:
            raise _validation("steps must be a non-empty array")
        for step in steps:
            if not isinstance(step, dict):
                raise _validation("each step must be a JSON object")
            action = step.get("action")
            if not isinstance(action, str) or not action.strip():
                raise _validation("each step must have a non-empty action")
        return copy.deepcopy(steps)

    @staticmethod
    def _validate_tags(tags: Any) -> list[str]:
        if tags is _MISSING or tags is None:
            return []
        if not isinstance(tags, list):
            raise _validation("tags must be an array")
        seen: set[str] = set()
        ordered: list[str] = []
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip():
                raise _validation("each tag must be a non-empty string")
            if tag not in seen:
                seen.add(tag)
                ordered.append(tag)
        return ordered

    def create_case(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = sorted(set(payload) - _CASE_FIELDS)
        if unknown:
            raise _validation(f"unknown fields: {', '.join(unknown)}")
        case_id = _required_string(payload, "id")
        name = _required_string(payload, "name")
        suite_id = _required_string(payload, "suite_id")
        kind = payload.get("kind", _MISSING)
        if kind is _MISSING:
            raise _validation("kind is required")
        if not isinstance(kind, str) or kind not in CASE_KINDS:
            raise _validation(f"kind must be one of: {', '.join(sorted(CASE_KINDS))}")
        steps = self._validate_steps(payload.get("steps", _MISSING))
        tags = self._validate_tags(payload.get("tags", _MISSING))
        enabled = payload.get("enabled", _MISSING)
        if enabled is _MISSING or enabled is None:
            enabled = True
        elif not isinstance(enabled, bool):
            raise _validation("enabled must be a boolean")
        timeout = payload.get("timeout_seconds", _MISSING)
        if timeout is _MISSING or timeout is None:
            timeout = DEFAULT_TIMEOUT_SECONDS
        elif isinstance(timeout, bool) or not isinstance(timeout, int):
            raise _validation("timeout_seconds must be an integer")
        elif not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise _validation(
                f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
            )
        with self._lock:
            if case_id in self._cases:
                raise ServiceError(409, "case_exists", f"case {case_id!r} already exists")
            if suite_id not in self._suites:
                raise ServiceError(404, "suite_not_found", f"suite {suite_id!r} not found")
            case = {
                "id": case_id,
                "name": name,
                "suite_id": suite_id,
                "kind": kind,
                "steps": steps,
                "tags": tags,
                "enabled": enabled,
                "timeout_seconds": timeout,
            }
            self._cases[case_id] = case
            return copy.deepcopy(case)

    def get_case(self, case_id: str) -> dict[str, Any]:
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise ServiceError(404, "case_not_found", f"case {case_id!r} not found")
            return copy.deepcopy(case)

    def list_cases(
        self,
        *,
        suite_id: str | None = None,
        include_descendants: bool = False,
        kind: str | None = None,
        enabled: bool | None = None,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Creation order, optionally filtered."""
        with self._lock:
            allowed_suites: set[str] | None = None
            if suite_id is not None:
                if suite_id not in self._suites:
                    raise ServiceError(
                        404, "suite_not_found", f"suite {suite_id!r} not found"
                    )
                allowed_suites = {suite_id}
                if include_descendants:
                    changed = True
                    while changed:
                        changed = False
                        for suite in self._suites.values():
                            if (
                                suite["parent_id"] in allowed_suites
                                and suite["id"] not in allowed_suites
                            ):
                                allowed_suites.add(suite["id"])
                                changed = True
            wanted_tags = set(tags) if tags else None
            result = []
            for case in self._cases.values():
                if allowed_suites is not None and case["suite_id"] not in allowed_suites:
                    continue
                if kind is not None and case["kind"] != kind:
                    continue
                if enabled is not None and case["enabled"] is not enabled:
                    continue
                if wanted_tags is not None and not wanted_tags <= set(case["tags"]):
                    continue
                result.append(copy.deepcopy(case))
            return result

    def delete_case(self, case_id: str) -> None:
        with self._lock:
            if case_id not in self._cases:
                raise ServiceError(404, "case_not_found", f"case {case_id!r} not found")
            del self._cases[case_id]
