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
MAX_RUN_CASE_IDS = 100
MAX_RUN_INSTANCES = 5000
RUN_OUTCOMES = ("passed", "failed", "error", "skipped")
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


def _validate_steps(value: object, field: str) -> list:
    """Step arrays share the case-step contract: objects with a non-empty action."""
    if not isinstance(value, list):
        raise _validation(f"{field} must be an array")
    for index, step in enumerate(value):
        if not isinstance(step, dict):
            raise _validation(f"{field}[{index}] must be a JSON object")
        action = step.get("action")
        if not isinstance(action, str) or action == "":
            raise _validation(f"{field}[{index}].action must be a non-empty string")
    return copy.deepcopy(value)


def _validate_references(value: object, field: str) -> list[str]:
    """Ordered list of unique non-empty string references (ids checked later)."""
    if not isinstance(value, list):
        raise _validation(f"{field} must be an array")
    cleaned: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise _validation(f"{field}[{index}] must be a non-empty string")
        if item in cleaned:
            raise _validation(f"{field} must not contain duplicates")
        cleaned.append(item)
    return cleaned


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
        self._fixtures: dict[str, dict] = {}
        self._snapshots: dict[str, dict] = {}
        self._runs: dict[str, dict] = {}

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
            "fixture_ids",
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
        steps = _validate_steps(steps, "steps")

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
            "steps": steps,
            "tags": tags,
            "enabled": enabled,
            "timeout_seconds": timeout,
        }
        if "parameterization" in payload and payload["parameterization"] is not None:
            case["parameterization"] = _validate_parameterization(
                payload["parameterization"]
            )
        if "fixture_ids" in payload and payload["fixture_ids"] is not None:
            case["fixture_ids"] = _validate_references(
                payload["fixture_ids"], "fixture_ids"
            )
        return case

    def create_case(self, payload: object) -> dict:
        case = self._validate_case(payload)
        with self._lock:
            if case["id"] in self._cases:
                raise ApiError(409, "case_exists", f"case {case['id']!r} already exists")
            if case["suite_id"] not in self._suites:
                raise ApiError(404, "suite_not_found", f"suite {case['suite_id']!r} not found")
            for fixture_id in case.get("fixture_ids", ()):
                if fixture_id not in self._fixtures:
                    raise ApiError(
                        404, "fixture_not_found", f"fixture {fixture_id!r} not found"
                    )
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

    # -- fixtures ---------------------------------------------------------

    def _validate_fixture(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        allowed = {"id", "name", "setup_steps", "teardown_steps", "dependencies"}
        unknown = set(payload) - allowed
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        for field in ("id", "name", "setup_steps", "teardown_steps"):
            if field not in payload:
                raise _validation(f"{field} is required")

        fixture_id = _clean_text(payload["id"], "id")
        name = _clean_text(payload["name"], "name")

        setup_steps = payload["setup_steps"]
        teardown_steps = payload["teardown_steps"]
        if not isinstance(setup_steps, list):
            raise _validation("setup_steps must be an array")
        if not isinstance(teardown_steps, list):
            raise _validation("teardown_steps must be an array")
        if not setup_steps and not teardown_steps:
            raise _validation("setup_steps and teardown_steps must not both be empty")

        fixture = {
            "id": fixture_id,
            "name": name,
            "setup_steps": _validate_steps(setup_steps, "setup_steps"),
            "teardown_steps": _validate_steps(teardown_steps, "teardown_steps"),
        }
        if "dependencies" in payload and payload["dependencies"] is not None:
            dependencies = _validate_references(payload["dependencies"], "dependencies")
            if fixture_id in dependencies:
                raise _validation("fixture must not depend on itself")
            fixture["dependencies"] = dependencies
        return fixture

    def create_fixture(self, payload: object) -> dict:
        fixture = self._validate_fixture(payload)
        with self._lock:
            if fixture["id"] in self._fixtures:
                raise ApiError(
                    409, "fixture_exists", f"fixture {fixture['id']!r} already exists"
                )
            for dependency in fixture.get("dependencies", ()):
                if dependency not in self._fixtures:
                    raise ApiError(
                        404, "fixture_not_found", f"fixture {dependency!r} not found"
                    )
            self._fixtures[fixture["id"]] = fixture
            return copy.deepcopy(fixture)

    def get_fixture(self, fixture_id: str) -> dict:
        with self._lock:
            fixture = self._fixtures.get(fixture_id)
            if fixture is None:
                raise ApiError(
                    404, "fixture_not_found", f"fixture {fixture_id!r} not found"
                )
            return copy.deepcopy(fixture)

    def list_fixtures(self) -> list[dict]:
        """Fixtures are returned in creation order."""
        with self._lock:
            return [copy.deepcopy(fixture) for fixture in self._fixtures.values()]

    def delete_fixture(self, fixture_id: str) -> None:
        with self._lock:
            if fixture_id not in self._fixtures:
                raise ApiError(
                    404, "fixture_not_found", f"fixture {fixture_id!r} not found"
                )
            for fixture in self._fixtures.values():
                if fixture_id in fixture.get("dependencies", ()):
                    raise ApiError(
                        409, "fixture_in_use", f"fixture {fixture_id!r} is still in use"
                    )
            for case in self._cases.values():
                if fixture_id in case.get("fixture_ids", ()):
                    raise ApiError(
                        409, "fixture_in_use", f"fixture {fixture_id!r} is still in use"
                    )
            del self._fixtures[fixture_id]

    # -- execution plan -----------------------------------------------------

    def _resolve_fixture_order(self, fixture_ids: list[str]) -> list[dict]:
        """Depth-first closure: dependencies before dependents, first visit wins."""
        ordered: list[dict] = []
        seen: set[str] = set()

        def visit(fixture_id: str) -> None:
            if fixture_id in seen:
                return
            seen.add(fixture_id)
            fixture = self._fixtures[fixture_id]
            for dependency in fixture.get("dependencies", ()):
                visit(dependency)
            ordered.append(fixture)

        for fixture_id in fixture_ids:
            visit(fixture_id)
        return ordered

    def get_execution_plan(self, case_id: str) -> dict:
        """Read-only preview of each instance's fixture setup/teardown order."""
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise ApiError(404, "case_not_found", f"case {case_id!r} not found")
            ordered = self._resolve_fixture_order(case.get("fixture_ids", ()))
            setup = [
                {"fixture_id": fixture["id"], "steps": copy.deepcopy(fixture["setup_steps"])}
                for fixture in ordered
            ]
            teardown = [
                {"fixture_id": fixture["id"], "steps": copy.deepcopy(fixture["teardown_steps"])}
                for fixture in reversed(ordered)
            ]
            parameters = _expand_parameterization(case.get("parameterization"))
            instances = [
                {
                    "id": f"{case_id}[{index}]",
                    "parameters": copy.deepcopy(values),
                    "setup": copy.deepcopy(setup),
                    "steps": copy.deepcopy(case["steps"]),
                    "teardown": copy.deepcopy(teardown),
                }
                for index, values in enumerate(parameters)
            ]
            return {"case_id": case_id, "count": len(instances), "instances": instances}

    # -- snapshots ---------------------------------------------------------

    def create_snapshot(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"id", "value"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        if "id" not in payload:
            raise _validation("id is required")
        if "value" not in payload:
            raise _validation("value is required")

        snapshot_id = _clean_text(payload["id"], "id")
        # `value` accepts any JSON value, including null; deepcopy isolates
        # stored state from both request and response objects.
        snapshot = {"id": snapshot_id, "value": copy.deepcopy(payload["value"])}
        with self._lock:
            if snapshot_id in self._snapshots:
                raise ApiError(
                    409,
                    "snapshot_exists",
                    f"snapshot {snapshot_id!r} already exists",
                )
            self._snapshots[snapshot_id] = snapshot
            return copy.deepcopy(snapshot)

    def get_snapshot(self, snapshot_id: str) -> dict:
        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                raise ApiError(
                    404,
                    "snapshot_not_found",
                    f"snapshot {snapshot_id!r} not found",
                )
            return copy.deepcopy(snapshot)

    def list_snapshots(self) -> list[dict]:
        """Snapshots are returned in creation order."""
        with self._lock:
            return [copy.deepcopy(snapshot) for snapshot in self._snapshots.values()]

    def delete_snapshot(self, snapshot_id: str) -> None:
        with self._lock:
            if snapshot_id not in self._snapshots:
                raise ApiError(
                    404,
                    "snapshot_not_found",
                    f"snapshot {snapshot_id!r} not found",
                )
            del self._snapshots[snapshot_id]

    def compare_snapshot(
        self, snapshot_id: str, actual: object, ignored: frozenset[str]
    ) -> dict:
        """Compare ``actual`` against the stored value without mutating it."""
        from .snapshots import compare_values

        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                raise ApiError(
                    404,
                    "snapshot_not_found",
                    f"snapshot {snapshot_id!r} not found",
                )
            expected = copy.deepcopy(snapshot["value"])
        result = compare_values(expected, actual, ignored)
        return {"snapshot_id": snapshot_id, **result}

    # -- runs --------------------------------------------------------------

    def _validate_run_request(self, payload: object) -> tuple[str, list[str]]:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"id", "case_ids"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        if "id" not in payload:
            raise _validation("id is required")
        if "case_ids" not in payload:
            raise _validation("case_ids is required")

        run_id = _clean_text(payload["id"], "id")
        raw_case_ids = payload["case_ids"]
        if not isinstance(raw_case_ids, list):
            raise _validation(f"case_ids must be an array of 1 to {MAX_RUN_CASE_IDS} items")
        if not 1 <= len(raw_case_ids) <= MAX_RUN_CASE_IDS:
            raise _validation(f"case_ids must contain between 1 and {MAX_RUN_CASE_IDS} items")
        case_ids: list[str] = []
        for index, item in enumerate(raw_case_ids):
            if not isinstance(item, str) or not item:
                raise _validation(f"case_ids[{index}] must be a non-empty string")
            if item in case_ids:
                raise _validation("case_ids must not contain duplicates")
            case_ids.append(item)
        return run_id, case_ids

    @staticmethod
    def _render_run(run: dict) -> dict:
        """Build the report; rows follow frozen instance order."""
        summary = {
            "total": len(run["instances"]),
            "pending": 0,
            "passed": 0,
            "failed": 0,
            "error": 0,
            "skipped": 0,
            "duration_ms": 0,
        }
        rows: list[dict] = []
        for entry in run["instances"]:
            row = {
                "instance_id": entry["instance_id"],
                "case_id": entry["case_id"],
                "name": copy.deepcopy(entry["name"]),
                "parameters": copy.deepcopy(entry["parameters"]),
            }
            result = entry["result"]
            if result is None:
                row["status"] = "pending"
                summary["pending"] += 1
            else:
                row["status"] = result["outcome"]
                row["outcome"] = result["outcome"]
                row["duration_ms"] = result["duration_ms"]
                if "details" in result:
                    row["details"] = copy.deepcopy(result["details"])
                summary[result["outcome"]] += 1
                summary["duration_ms"] += result["duration_ms"]
            rows.append(row)
        passed = (
            run["status"] == "completed"
            and summary["failed"] == 0
            and summary["error"] == 0
        )
        return {
            "id": run["id"],
            "status": run["status"],
            "passed": passed,
            "summary": summary,
            "instances": rows,
        }

    def create_run(self, payload: object) -> dict:
        run_id, case_ids = self._validate_run_request(payload)
        with self._lock:
            if run_id in self._runs:
                raise ApiError(409, "run_exists", f"run {run_id!r} already exists")

            # Resolve every case before freezing anything so a rejected
            # request never leaves a partial run behind.
            resolved: list[tuple[dict, list[dict]]] = []
            total = 0
            for case_id in case_ids:
                case = self._cases.get(case_id)
                if case is None:
                    raise ApiError(404, "case_not_found", f"case {case_id!r} not found")
                if not case["enabled"]:
                    raise ApiError(409, "case_disabled", f"case {case_id!r} is disabled")
                parameters = _expand_parameterization(case.get("parameterization"))
                total += len(parameters)
                resolved.append((case, parameters))
            if total > MAX_RUN_INSTANCES:
                raise _validation(
                    f"run expands to {total} instances; limit is {MAX_RUN_INSTANCES}"
                )

            # Freeze name, instance id and parameters; later catalog changes
            # (rename, delete, rebuild, different parameterization) never
            # touch the run.
            instances: list[dict] = []
            for case, parameters in resolved:
                for index, values in enumerate(parameters):
                    instances.append(
                        {
                            "instance_id": f"{case['id']}[{index}]",
                            "case_id": case["id"],
                            "name": copy.deepcopy(case["name"]),
                            "parameters": copy.deepcopy(values),
                            "result": None,
                        }
                    )
            run = {"id": run_id, "status": "open", "instances": instances}
            self._runs[run_id] = run
            return self._render_run(run)

    def get_run(self, run_id: str) -> dict:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            return self._render_run(run)

    @staticmethod
    def _validate_result(payload: object) -> tuple[str, str, int, object, bool]:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"instance_id", "outcome", "duration_ms", "details"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        for field in ("instance_id", "outcome", "duration_ms"):
            if field not in payload:
                raise _validation(f"{field} is required")

        instance_id = payload["instance_id"]
        if not isinstance(instance_id, str) or not instance_id:
            raise _validation("instance_id must be a non-empty string")
        outcome = payload["outcome"]
        if not isinstance(outcome, str) or outcome not in RUN_OUTCOMES:
            raise _validation(f"outcome must be one of: {', '.join(RUN_OUTCOMES)}")
        duration = payload["duration_ms"]
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise _validation("duration_ms must be a non-negative integer")
        return instance_id, outcome, duration, payload.get("details"), "details" in payload

    def submit_result(self, run_id: str, payload: object) -> dict:
        instance_id, outcome, duration, details, has_details = self._validate_result(payload)
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] == "completed":
                raise ApiError(409, "run_completed", f"run {run_id!r} is completed")
            entry = next(
                (item for item in run["instances"] if item["instance_id"] == instance_id),
                None,
            )
            if entry is None:
                raise ApiError(
                    404,
                    "instance_not_found",
                    f"instance {instance_id!r} not found in run {run_id!r}",
                )
            if entry["result"] is not None:
                raise ApiError(
                    409,
                    "result_exists",
                    f"result for instance {instance_id!r} already exists",
                )
            result: dict = {"outcome": outcome, "duration_ms": duration}
            if has_details:
                result["details"] = copy.deepcopy(details)
            entry["result"] = result
            return self._render_run(run)

    def complete_run(self, run_id: str) -> dict:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] == "completed":
                raise ApiError(409, "run_completed", f"run {run_id!r} is completed")
            if any(item["result"] is None for item in run["instances"]):
                raise ApiError(
                    409,
                    "run_incomplete",
                    f"run {run_id!r} still has pending instances",
                )
            run["status"] = "completed"
            return self._render_run(run)
