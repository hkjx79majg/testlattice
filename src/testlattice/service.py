"""Core service surface for TestLattice.

The catalog keeps suites and cases in memory; all state is lost when the
process restarts. Access is guarded by a lock because the HTTP layer runs
on a threading server.
"""

from __future__ import annotations

import copy
import math
import re
import secrets
import threading
import time
from collections.abc import Mapping

from . import __version__

ALLOWED_KINDS = ("unit", "api", "browser", "contract")
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 86400
MAX_INSTANCES = 1000
MAX_RUN_CASES = 100
MAX_RUN_INSTANCES = 5000
MAX_AGGREGATE_RUNS = 100
MAX_COVERAGE_FILES = 1000
MAX_COVERAGE_EXECUTABLE_LINES = 100000
DEFAULT_CLAIM_MAX_ITEMS = 1
MAX_CLAIM_ITEMS = 100
MIN_LEASE_SECONDS = 1
MAX_LEASE_SECONDS = 3600
ALLOWED_OUTCOMES = ("passed", "failed", "error", "skipped")
RETRY_OUTCOMES = ("failed", "error")
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


def _now_ms() -> int:
    """Current wall-clock time as an integer number of epoch milliseconds."""
    return time.time_ns() // 1_000_000


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


def _validate_line_numbers(value: object, field: str, *, allow_empty: bool) -> list[int]:
    """Unique positive integers in ascending order; request order is normalized."""
    if not isinstance(value, list):
        raise _validation(f"{field} must be an array")
    if not allow_empty and not value:
        raise _validation(f"{field} must not be empty")
    lines: list[int] = []
    seen: set[int] = set()
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            raise _validation(f"{field}[{index}] must be a positive integer")
        if item < 1:
            raise _validation(f"{field}[{index}] must be a positive integer")
        if item in seen:
            raise _validation(f"{field} must not contain duplicates")
        seen.add(item)
        lines.append(item)
    lines.sort()
    return lines


def _validate_coverage(value: object) -> dict[str, dict[str, list[int]]]:
    """Validate the optional coverage fragment attached to one instance result.

    Only ``files`` is allowed: a non-empty object keyed by non-empty file
    paths whose values contain exactly ``executable_lines`` (a non-empty
    unique positive-int array) and ``covered_lines`` (a possibly empty
    unique positive-int array that must be a subset). Limited to
    MAX_COVERAGE_FILES files and MAX_COVERAGE_EXECUTABLE_LINES executable
    line numbers in total. Stored as normalized {path: {executable,
    covered}} sets; request file/line order is never significant.
    """
    if not isinstance(value, dict):
        raise _validation("coverage must be a JSON object")
    unknown = set(value) - {"files"}
    if unknown:
        raise _validation(
            f"coverage unknown fields: {', '.join(sorted(unknown))}"
        )
    if "files" not in value:
        raise _validation("coverage.files is required")
    files = value["files"]
    if not isinstance(files, dict) or not files:
        raise _validation("coverage.files must be a non-empty object")
    if len(files) > MAX_COVERAGE_FILES:
        raise _validation(
            f"coverage must contain at most {MAX_COVERAGE_FILES} files"
        )

    normalized: dict[str, dict[str, list[int]]] = {}
    total_executable = 0
    for path, entry in files.items():
        if not isinstance(path, str) or path == "":
            raise _validation("coverage file paths must be non-empty strings")
        if not isinstance(entry, dict):
            raise _validation(f"coverage.files.{path} must be a JSON object")
        entry_unknown = set(entry) - {"executable_lines", "covered_lines"}
        if entry_unknown:
            raise _validation(
                f"coverage.files.{path} unknown fields: "
                f"{', '.join(sorted(entry_unknown))}"
            )
        if "executable_lines" not in entry:
            raise _validation(
                f"coverage.files.{path}.executable_lines is required"
            )
        if "covered_lines" not in entry:
            raise _validation(
                f"coverage.files.{path}.covered_lines is required"
            )
        executable = _validate_line_numbers(
            entry["executable_lines"],
            f"coverage.files.{path}.executable_lines",
            allow_empty=False,
        )
        covered = _validate_line_numbers(
            entry["covered_lines"],
            f"coverage.files.{path}.covered_lines",
            allow_empty=True,
        )
        if not set(covered).issubset(executable):
            raise _validation(
                f"coverage.files.{path}.covered_lines must be a subset of "
                "executable_lines"
            )
        total_executable += len(executable)
        if total_executable > MAX_COVERAGE_EXECUTABLE_LINES:
            raise _validation(
                "coverage must contain at most "
                f"{MAX_COVERAGE_EXECUTABLE_LINES} executable line numbers in total"
            )
        normalized[path] = {"executable_lines": executable, "covered_lines": covered}
    return normalized


def _merge_coverage(
    target: dict[str, dict[str, set[int]]], fragment: dict[str, dict[str, list[int]]]
) -> None:
    """Union one validated fragment into the run-level coverage accumulator."""
    for path, entry in fragment.items():
        merged = target.get(path)
        if merged is None:
            merged = target[path] = {"executable_lines": set(), "covered_lines": set()}
        merged["executable_lines"].update(entry["executable_lines"])
        merged["covered_lines"].update(entry["covered_lines"])


def _coverage_percent(covered: int, executable: int) -> float:
    """covered / executable * 100 rounded half-up to two decimals.

    Computed with integer arithmetic so the x.xx5 tie rounds up
    (四舍五入) rather than following binary/banker's rounding.
    """
    hundredths = covered * 10000 // executable
    remainder = covered * 10000 % executable
    if remainder * 2 >= executable:
        hundredths += 1
    return hundredths / 100


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


def _empty_aggregate_summary() -> dict:
    return {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "error": 0,
        "skipped": 0,
        "duration_ms": 0,
    }


def _accumulate(summary: dict, instance: dict) -> None:
    summary["total"] += 1
    summary[instance["outcome"]] += 1
    summary["duration_ms"] += instance["duration_ms"]


def _merge_into(target: dict, source: dict) -> None:
    for key, value in source.items():
        target[key] += value


def _summary_passed(summary: dict) -> bool:
    return summary["failed"] == 0 and summary["error"] == 0


def _trend(flags: list[bool]) -> str:
    """Classify the per-run pass/fail sequence of one case."""
    if len(flags) < 2:
        return "insufficient"
    if all(flags):
        return "stable_pass"
    if not any(flags):
        return "stable_fail"
    if flags[0] and not flags[-1]:
        return "regression"
    if not flags[0] and flags[-1]:
        return "improvement"
    return "fluctuating"


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
        # Run id -> {path: {"executable_lines": set[int], "covered_lines": set[int]}}
        self._coverage: dict[str, dict[str, dict[str, set[int]]]] = {}

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

    # -- runs -------------------------------------------------------------

    def _freeze_case_context(self, case: dict) -> dict:
        """Freeze one case's reproducible context using execution-plan semantics.

        Setup lists dependencies before dependents (first visit wins) and
        teardown is the strict reverse; the case steps and timeout/kind are
        copied so later catalog edits or deletions cannot affect the run.
        """
        ordered = self._resolve_fixture_order(case.get("fixture_ids", ()))
        setup = [
            {"fixture_id": fixture["id"], "steps": copy.deepcopy(fixture["setup_steps"])}
            for fixture in ordered
        ]
        teardown = [
            {"fixture_id": fixture["id"], "steps": copy.deepcopy(fixture["teardown_steps"])}
            for fixture in reversed(ordered)
        ]
        return {
            "kind": case["kind"],
            "timeout_seconds": case["timeout_seconds"],
            "setup": setup,
            "steps": copy.deepcopy(case["steps"]),
            "teardown": teardown,
        }

    def create_run(self, payload: object) -> dict:
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
        case_ids = _validate_references(payload["case_ids"], "case_ids")
        if not 1 <= len(case_ids) <= MAX_RUN_CASES:
            raise _validation(
                f"case_ids must contain between 1 and {MAX_RUN_CASES} entries"
            )

        with self._lock:
            if run_id in self._runs:
                raise ApiError(409, "run_exists", f"run {run_id!r} already exists")
            # Freeze case names, instance ids, parameters and the full
            # reproducible context (kind, timeout, steps, fixture plan) at
            # creation time; later catalog changes must not affect the record.
            instances: list[dict] = []
            contexts: list[dict] = []
            for case_id in case_ids:
                case = self._cases.get(case_id)
                if case is None:
                    raise ApiError(
                        404, "case_not_found", f"case {case_id!r} not found"
                    )
                if not case["enabled"]:
                    raise ApiError(
                        409, "case_disabled", f"case {case_id!r} is disabled"
                    )
                context = self._freeze_case_context(case)
                parameters = _expand_parameterization(case.get("parameterization"))
                for index, values in enumerate(parameters):
                    instances.append(
                        {
                            "instance_id": f"{case_id}[{index}]",
                            "case_id": case_id,
                            "case_name": case["name"],
                            "parameters": copy.deepcopy(values),
                            "outcome": "pending",
                        }
                    )
                    contexts.append(copy.deepcopy(context))
            if len(instances) > MAX_RUN_INSTANCES:
                raise _validation(
                    f"run expands to {len(instances)} instances; "
                    f"limit is {MAX_RUN_INSTANCES}"
                )
            run = {
                "id": run_id,
                "status": "open",
                "instances": instances,
                "contexts": contexts,
            }
            self._runs[run_id] = run
            return self._run_report(run)

    def get_run(self, run_id: str) -> dict:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            return self._run_report(run)

    def get_run_junit(self, run_id: str) -> dict:
        """Return the frozen report for a completed run; rendering happens in
        the caller so the stored record is never mutated."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] != "completed":
                raise ApiError(
                    409, "run_incomplete", f"run {run_id!r} is not completed"
                )
            return self._run_report(run)

    def get_run_coverage(self, run_id: str) -> dict:
        """Read-only merged coverage report for a completed run.

        Unions the executable and covered line sets of every fragment
        submitted to this run; nothing is mutated, so repeated reads of the
        same completed run return byte-identical order and counts.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] != "completed":
                raise ApiError(
                    409, "run_incomplete", f"run {run_id!r} is not completed"
                )
            accumulated = self._coverage.get(run_id, {})

            files: list[dict] = []
            total_executable = 0
            total_covered = 0
            for path in sorted(accumulated):
                entry = accumulated[path]
                executable = sorted(entry["executable_lines"])
                covered_set = entry["covered_lines"]
                covered = [line for line in executable if line in covered_set]
                missed = [line for line in executable if line not in covered_set]
                files.append(
                    {
                        "path": path,
                        "executable_lines": executable,
                        "covered_lines": covered,
                        "missed_lines": missed,
                        "coverage_percent": _coverage_percent(
                            len(covered), len(executable)
                        ),
                    }
                )
                total_executable += len(executable)
                total_covered += len(covered)

            total_missed = total_executable - total_covered
            percent = (
                _coverage_percent(total_covered, total_executable)
                if total_executable
                else None
            )
            return {
                "run_id": run_id,
                "summary": {
                    "files": len(files),
                    "executable_lines": total_executable,
                    "covered_lines": total_covered,
                    "missed_lines": total_missed,
                    "coverage_percent": percent,
                },
                "files": files,
            }

    def get_run_diagnostics(self, run_id: str) -> dict:
        """Read-only failure diagnostics export for a completed run.

        Uses only the frozen run record and per-instance execution contexts;
        the catalog is never consulted and nothing is mutated, so repeated
        reads return identical content and array order.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] != "completed":
                raise ApiError(
                    409, "run_incomplete", f"run {run_id!r} is not completed"
                )

            failures: list[dict] = []
            failed_count = 0
            error_count = 0
            for instance, context in zip(run["instances"], run["contexts"]):
                outcome = instance["outcome"]
                if outcome not in RETRY_OUTCOMES:
                    continue
                if outcome == "failed":
                    failed_count += 1
                else:
                    error_count += 1
                failure = {
                    "instance_id": copy.deepcopy(instance["instance_id"]),
                    "case_id": copy.deepcopy(instance["case_id"]),
                    "case_name": copy.deepcopy(instance["case_name"]),
                    "kind": copy.deepcopy(context["kind"]),
                    "parameters": copy.deepcopy(instance["parameters"]),
                    "outcome": copy.deepcopy(outcome),
                    "duration_ms": instance["duration_ms"],
                    "timeout_seconds": context["timeout_seconds"],
                    "setup": copy.deepcopy(context["setup"]),
                    "steps": copy.deepcopy(context["steps"]),
                    "teardown": copy.deepcopy(context["teardown"]),
                }
                if "details" in instance:
                    failure["details"] = copy.deepcopy(instance["details"])
                failures.append(failure)

            report = {
                "run_id": run_id,
                "summary": {
                    "total": failed_count + error_count,
                    "failed": failed_count,
                    "error": error_count,
                },
                "failures": failures,
            }
            if "retry_of" in run:
                report["retry_of"] = run["retry_of"]
                report["root_run_id"] = run["root_run_id"]
                report["attempt"] = run["attempt"]
            return report

    # -- leases / claims --------------------------------------------------

    @staticmethod
    def _lease_active(lease: object, now_ms: int) -> bool:
        """A lease is valid until (but not once) current time reaches expiry."""
        return isinstance(lease, dict) and not lease.get("consumed") and lease["expires_at"] > now_ms

    def _drop_expired_leases(self, run: dict, now_ms: int) -> None:
        """Expired leases vanish silently; the instances stay pending and
        carry no result, duration or coverage, and become claimable again."""
        for instance in run["instances"]:
            lease = instance.get("lease")
            if lease is not None and not self._lease_active(lease, now_ms):
                instance.pop("lease", None)

    @staticmethod
    def _claim_view(instance: dict, context: dict, lease: dict) -> dict:
        return {
            "claim_id": lease["claim_id"],
            "instance_id": copy.deepcopy(instance["instance_id"]),
            "case_id": copy.deepcopy(instance["case_id"]),
            "case_name": copy.deepcopy(instance["case_name"]),
            "kind": copy.deepcopy(context["kind"]),
            "parameters": copy.deepcopy(instance["parameters"]),
            "timeout_seconds": context["timeout_seconds"],
            "setup": copy.deepcopy(context["setup"]),
            "steps": copy.deepcopy(context["steps"]),
            "teardown": copy.deepcopy(context["teardown"]),
            "expires_at": lease["expires_at"],
        }

    def claim_instances(self, run_id: str, payload: object) -> dict:
        """Lease pending instances of one run to one worker.

        Selection follows frozen order and skips every instance that still
        holds an unexpired, unconsumed lease. The whole scan-and-lease step
        runs under the service lock, so concurrent claims can never attach
        two active leases to the same instance.
        """
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"worker_id", "max_items", "lease_seconds"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        if "worker_id" not in payload:
            raise _validation("worker_id is required")
        worker_id = _clean_text(payload["worker_id"], "worker_id")

        max_items = DEFAULT_CLAIM_MAX_ITEMS
        if "max_items" in payload:
            value = payload["max_items"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise _validation("max_items must be an integer")
            if not 1 <= value <= MAX_CLAIM_ITEMS:
                raise _validation(
                    f"max_items must be between 1 and {MAX_CLAIM_ITEMS}"
                )
            max_items = value

        if "lease_seconds" not in payload:
            raise _validation("lease_seconds is required")
        lease_seconds = payload["lease_seconds"]
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise _validation("lease_seconds must be an integer")
        if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
            raise _validation(
                f"lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
            )

        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] == "completed":
                raise ApiError(
                    409, "run_completed", f"run {run_id!r} is already completed"
                )
            now_ms = _now_ms()
            self._drop_expired_leases(run, now_ms)
            expires_at = now_ms + lease_seconds * 1000

            claims: list[dict] = []
            for instance, context in zip(run["instances"], run["contexts"]):
                if len(claims) >= max_items:
                    break
                if instance["outcome"] != "pending" or "lease" in instance:
                    continue
                claim_id = secrets.token_urlsafe(18)
                lease = {
                    "claim_id": claim_id,
                    "worker_id": worker_id,
                    "claimed_at": now_ms,
                    "expires_at": expires_at,
                    "consumed": False,
                }
                instance["lease"] = lease
                claims.append(self._claim_view(instance, context, lease))
            return {"claims": claims}

    def submit_result(self, run_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {
            "instance_id",
            "outcome",
            "duration_ms",
            "details",
            "coverage",
            "claim_id",
        }
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        for field in ("instance_id", "outcome", "duration_ms"):
            if field not in payload:
                raise _validation(f"{field} is required")

        instance_id = _reference(payload["instance_id"], "instance_id")
        outcome = payload["outcome"]
        if not isinstance(outcome, str) or outcome not in ALLOWED_OUTCOMES:
            raise _validation(f"outcome must be one of: {', '.join(ALLOWED_OUTCOMES)}")
        duration_ms = payload["duration_ms"]
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
            raise _validation("duration_ms must be an integer")
        if duration_ms < 0:
            raise _validation("duration_ms must be non-negative")
        has_claim = "claim_id" in payload
        claim_id = payload.get("claim_id")
        if has_claim:
            claim_id = _reference(claim_id, "claim_id")
        has_details = "details" in payload
        details = copy.deepcopy(payload.get("details"))
        # Fully validated before any run state is touched, so a malformed
        # fragment leaves both the result and coverage unwritten.
        coverage = (
            _validate_coverage(payload["coverage"]) if "coverage" in payload else None
        )

        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] == "completed":
                raise ApiError(
                    409, "run_completed", f"run {run_id!r} is already completed"
                )
            instance = next(
                (
                    item
                    for item in run["instances"]
                    if item["instance_id"] == instance_id
                ),
                None,
            )
            if instance is None:
                raise ApiError(
                    404,
                    "instance_not_found",
                    f"instance {instance_id!r} not found in run {run_id!r}",
                )
            now_ms = _now_ms()
            self._drop_expired_leases(run, now_ms)
            lease = instance.get("lease")
            if lease is not None:
                # A leased instance only accepts its own live claim. Offering
                # another still-active lease (of any instance in the run) is
                # a conflict; expired/consumed/unknown/foreign ids are simply
                # not active.
                if not has_claim:
                    raise ApiError(
                        409,
                        "claim_conflict",
                        f"instance {instance_id!r} is held by an active claim",
                    )
                if claim_id != lease["claim_id"]:
                    other_active = any(
                        other is not instance
                        and isinstance(other.get("lease"), dict)
                        and other["lease"]["claim_id"] == claim_id
                        for other in run["instances"]
                    )
                    if other_active:
                        raise ApiError(
                            409,
                            "claim_conflict",
                            f"claim {claim_id!r} is active for another instance",
                        )
                    raise ApiError(
                        409,
                        "claim_not_active",
                        f"claim {claim_id!r} is not active for instance {instance_id!r}",
                    )
            elif has_claim:
                raise ApiError(
                    409,
                    "claim_not_active",
                    f"claim {claim_id!r} is not active for instance {instance_id!r}",
                )
            if instance["outcome"] != "pending":
                raise ApiError(
                    409,
                    "result_exists",
                    f"instance {instance_id!r} already has a result",
                )
            # Consume the lease and write the result atomically: a failed
            # request above leaves both the lease and the instance untouched.
            if lease is not None:
                lease["consumed"] = True
                instance.pop("lease", None)
            instance["outcome"] = outcome
            instance["duration_ms"] = duration_ms
            if has_details:
                instance["details"] = details
            if coverage is not None:
                accumulated = self._coverage.setdefault(run_id, {})
                _merge_coverage(accumulated, coverage)
            return self._run_report(run)

    def check_timeouts(self, run_id: str, payload: object) -> dict:
        """Fail leased instances whose frozen timeout has elapsed.

        The request body must be an empty JSON object. Under the service
        lock, leases already invalid by ``expires_at`` are dropped first
        (they only make the instance claimable again); then every pending
        instance still holding a live lease whose ``claimed_at`` plus the
        frozen ``timeout_seconds`` has been reached is timed out
        atomically: the lease is consumed and an ``error`` result with
        fixed timeout details is written. Instances that already have a
        result, hold no lease or hold a consumed/expired lease are left
        untouched, and the run is never auto-completed.
        """
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        if payload:
            raise _validation(f"unknown fields: {', '.join(sorted(payload))}")

        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] == "completed":
                raise ApiError(
                    409, "run_completed", f"run {run_id!r} is already completed"
                )
            now_ms = _now_ms()
            self._drop_expired_leases(run, now_ms)

            timed_out: list[str] = []
            for instance, context in zip(run["instances"], run["contexts"]):
                if instance["outcome"] != "pending":
                    continue
                lease = instance.get("lease")
                if lease is None:
                    continue
                timeout_seconds = context["timeout_seconds"]
                timeout_ms = timeout_seconds * 1000
                deadline_at = lease["claimed_at"] + timeout_ms
                if now_ms < deadline_at:
                    continue
                # Consume the lease and write the timeout result atomically,
                # so a concurrent submit with the old claim id loses.
                instance.pop("lease", None)
                instance["outcome"] = "error"
                instance["duration_ms"] = timeout_ms
                instance["details"] = {
                    "code": "timeout",
                    "timeout_seconds": timeout_seconds,
                    "worker_id": lease["worker_id"],
                    "claimed_at": lease["claimed_at"],
                    "deadline_at": deadline_at,
                    "detected_at": now_ms,
                }
                timed_out.append(instance["instance_id"])
            return {"timed_out": timed_out, "run": self._run_report(run)}

    def complete_run(self, run_id: str) -> dict:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] == "completed":
                raise ApiError(
                    409, "run_completed", f"run {run_id!r} is already completed"
                )
            if any(item["outcome"] == "pending" for item in run["instances"]):
                raise ApiError(
                    409, "run_incomplete", f"run {run_id!r} still has pending instances"
                )
            run["status"] = "completed"
            return self._run_report(run)

    def retry_run(self, source_run_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"id", "outcomes"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        if "id" not in payload:
            raise _validation("id is required")

        run_id = _clean_text(payload["id"], "id")
        if "outcomes" not in payload:
            outcomes = list(RETRY_OUTCOMES)
        else:
            outcomes = self._validate_retry_outcomes(payload["outcomes"])

        with self._lock:
            source = self._runs.get(source_run_id)
            if source is None:
                raise ApiError(
                    404, "run_not_found", f"run {source_run_id!r} not found"
                )
            if source["status"] != "completed":
                raise ApiError(
                    409, "run_incomplete", f"run {source_run_id!r} is not completed"
                )

            # Select straight from the frozen source report in its frozen
            # order; the catalog is never consulted when retrying. Each
            # selected instance keeps the source's frozen reproducible
            # context, so chained retries never re-read the catalog either.
            selected = [
                item
                for item in source["instances"]
                if item["outcome"] in outcomes
            ]
            selected_contexts = [
                copy.deepcopy(source["contexts"][index])
                for index, item in enumerate(source["instances"])
                if item["outcome"] in outcomes
            ]
            if not selected:
                raise ApiError(
                    409,
                    "retry_not_needed",
                    f"run {source_run_id!r} has no instances with outcome "
                    f"{', '.join(outcomes)}",
                )
            if run_id in self._runs:
                raise ApiError(409, "run_exists", f"run {run_id!r} already exists")

            instances = [
                {
                    "instance_id": copy.deepcopy(item["instance_id"]),
                    "case_id": copy.deepcopy(item["case_id"]),
                    "case_name": copy.deepcopy(item["case_name"]),
                    "parameters": copy.deepcopy(item["parameters"]),
                    "outcome": "pending",
                }
                for item in selected
            ]
            source_attempt = source.get("attempt")
            attempt = source_attempt + 1 if source_attempt is not None else 1
            root_run_id = (
                source.get("root_run_id")
                if source_attempt is not None
                else source_run_id
            )
            run = {
                "id": run_id,
                "status": "open",
                "instances": instances,
                "contexts": selected_contexts,
                "retry_of": source_run_id,
                "root_run_id": root_run_id,
                "attempt": attempt,
            }
            self._runs[run_id] = run
            return self._run_report(run)

    @staticmethod
    def _validate_retry_outcomes(value: object) -> list[str]:
        if not isinstance(value, list):
            raise _validation("outcomes must be an array")
        if not value:
            raise _validation("outcomes must not be empty")
        cleaned: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or item not in RETRY_OUTCOMES:
                raise _validation(
                    f"outcomes[{index}] must be one of: {', '.join(RETRY_OUTCOMES)}"
                )
            if item in cleaned:
                raise _validation("outcomes must not contain duplicates")
            cleaned.append(item)
        return cleaned

    # -- report aggregation ------------------------------------------------

    def aggregate_reports(self, payload: object) -> dict:
        """Read-only cross-run aggregation over completed runs.

        Uses only the frozen run records; the catalog is never consulted and
        nothing is mutated.
        """
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"run_ids"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        if "run_ids" not in payload:
            raise _validation("run_ids is required")
        run_ids = _validate_references(payload["run_ids"], "run_ids")
        if not 1 <= len(run_ids) <= MAX_AGGREGATE_RUNS:
            raise _validation(
                f"run_ids must contain between 1 and {MAX_AGGREGATE_RUNS} entries"
            )

        with self._lock:
            runs: list[dict] = []
            for run_id in run_ids:
                run = self._runs.get(run_id)
                if run is None:
                    raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
                if run["status"] != "completed":
                    raise ApiError(
                        409, "run_incomplete", f"run {run_id!r} is not completed"
                    )
                runs.append(run)

            total_summary = _empty_aggregate_summary()
            run_entries: list[dict] = []
            case_order: list[str] = []
            case_entries: dict[str, dict] = {}

            for run in runs:
                run_summary = _empty_aggregate_summary()
                # Per-case slices of this run, in frozen first-appearance order.
                slices: dict[str, dict] = {}
                for instance in run["instances"]:
                    _accumulate(run_summary, instance)
                    case_id = instance["case_id"]
                    slice_ = slices.get(case_id)
                    if slice_ is None:
                        slice_ = slices[case_id] = {
                            "case_name": instance["case_name"],
                            "summary": _empty_aggregate_summary(),
                        }
                    _accumulate(slice_["summary"], instance)

                entry = {
                    "run_id": run["id"],
                    "passed": _summary_passed(run_summary),
                    "summary": run_summary,
                }
                if "retry_of" in run:
                    entry["retry_of"] = run["retry_of"]
                    entry["root_run_id"] = run["root_run_id"]
                    entry["attempt"] = run["attempt"]
                run_entries.append(entry)
                _merge_into(total_summary, run_summary)

                for case_id, slice_ in slices.items():
                    case_entry = case_entries.get(case_id)
                    if case_entry is None:
                        case_entry = case_entries[case_id] = {
                            "case_id": case_id,
                            "summary": _empty_aggregate_summary(),
                            "runs": [],
                            "flags": [],
                        }
                        case_order.append(case_id)
                    _merge_into(case_entry["summary"], slice_["summary"])
                    passed = _summary_passed(slice_["summary"])
                    case_entry["flags"].append(passed)
                    case_entry["runs"].append(
                        {
                            "run_id": run["id"],
                            "case_name": slice_["case_name"],
                            "summary": slice_["summary"],
                            "passed": passed,
                        }
                    )

            cases = [
                {
                    "case_id": case_id,
                    "summary": case_entries[case_id]["summary"],
                    "trend": _trend(case_entries[case_id]["flags"]),
                    "runs": case_entries[case_id]["runs"],
                }
                for case_id in case_order
            ]
            return {
                "run_count": len(run_entries),
                "passed": _summary_passed(total_summary),
                "summary": total_summary,
                "runs": run_entries,
                "cases": cases,
            }

    @staticmethod
    def _run_report(run: dict) -> dict:
        summary = {
            "total": len(run["instances"]),
            "pending": 0,
            "passed": 0,
            "failed": 0,
            "error": 0,
            "skipped": 0,
            "duration_ms": 0,
        }
        public_instances: list[dict] = []
        for instance in run["instances"]:
            outcome = instance["outcome"]
            summary[outcome] += 1
            if outcome != "pending":
                summary["duration_ms"] += instance["duration_ms"]
            # Leases are internal scheduling state and never appear in reports.
            public_instances.append(
                {
                    key: copy.deepcopy(value)
                    for key, value in instance.items()
                    if key != "lease"
                }
            )
        passed = (
            run["status"] == "completed"
            and summary["failed"] == 0
            and summary["error"] == 0
        )
        report = {
            "id": run["id"],
            "status": run["status"],
            "passed": passed,
            "instances": public_instances,
            "summary": summary,
        }
        if "retry_of" in run:
            report["retry_of"] = run["retry_of"]
            report["root_run_id"] = run["root_run_id"]
            report["attempt"] = run["attempt"]
        return report
