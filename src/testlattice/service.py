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
MIN_STABILITY_RUNS = 2
MAX_STABILITY_RUNS = 100
GATE_POLICY_FIELDS = (
    "max_failed",
    "max_error",
    "min_coverage_percent",
    "max_flaky",
)
GATE_INTEGER_FIELDS = ("max_failed", "max_error", "max_flaky")
MIN_GATE_FLAKY_RUNS = 2
MAX_COVERAGE_FILES = 1000
MAX_COVERAGE_EXECUTABLE_LINES = 100000
DEFAULT_CLAIM_MAX_ITEMS = 1
MAX_CLAIM_ITEMS = 100
MIN_LEASE_SECONDS = 1
MAX_LEASE_SECONDS = 3600
MIN_HEARTBEAT_SECONDS = 1
MAX_HEARTBEAT_SECONDS = 300
MIN_POOL_CAPACITY = 1
MAX_POOL_CAPACITY = 10000
ALLOWED_OUTCOMES = ("passed", "failed", "error", "skipped")
RETRY_OUTCOMES = ("failed", "error")
ARCHIVE_VERSION = 1
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


def _validate_resource_requirements(value: object) -> dict[str, int]:
    """Object mapping resource pool ids to positive integer amounts."""
    if not isinstance(value, dict):
        raise _validation("resource_requirements must be a JSON object")
    cleaned: dict[str, int] = {}
    for pool_id, amount in value.items():
        if not isinstance(pool_id, str) or pool_id == "":
            raise _validation(
                "resource_requirements keys must be non-empty strings"
            )
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise _validation(
                f"resource_requirements.{pool_id} must be a positive integer"
            )
        if amount < 1:
            raise _validation(
                f"resource_requirements.{pool_id} must be a positive integer"
            )
        cleaned[pool_id] = amount
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


def _rounded_rate(numerator: int, denominator: int) -> float:
    """numerator / denominator rounded half-up to two decimals.

    Same integer half-up rule as ``_coverage_percent``, but the result is
    a ratio in [0, 1] rather than a percentage.
    """
    hundredths = numerator * 100 // denominator
    remainder = numerator * 100 % denominator
    if remainder * 2 >= denominator:
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


# -- run archives -------------------------------------------------------

def _archive_context_view(instance: dict, context: dict) -> dict:
    """Archive shape of one frozen instance context.

    Mirrors the claim/diagnostics view: frozen case name, kind, parameters,
    timeout, fixture orchestration, case steps and resource requirements
    (the latter omitted when empty).
    """
    view = {
        "case_name": copy.deepcopy(instance["case_name"]),
        "kind": copy.deepcopy(context["kind"]),
        "parameters": copy.deepcopy(instance["parameters"]),
        "timeout_seconds": context["timeout_seconds"],
        "setup": copy.deepcopy(context["setup"]),
        "steps": copy.deepcopy(context["steps"]),
        "teardown": copy.deepcopy(context["teardown"]),
    }
    requirements = context.get("resource_requirements") or {}
    if requirements:
        view["resource_requirements"] = copy.deepcopy(requirements)
    return view


def _archive_coverage_files(
    accumulated: dict[str, dict[str, set[int]]],
) -> list[dict]:
    """Normalized merged coverage for an archive: files ordered by Unicode
    code point of path, line number arrays strictly ascending, covered lines
    a subset of executable lines."""
    files: list[dict] = []
    for path in sorted(accumulated):
        entry = accumulated[path]
        files.append(
            {
                "path": path,
                "executable_lines": sorted(entry["executable_lines"]),
                "covered_lines": sorted(entry["covered_lines"]),
            }
        )
    return files


def _archive_fixture_groups(value: object, field: str) -> list[dict]:
    """Validate a setup/teardown group array: {"fixture_id", "steps"} items."""
    if not isinstance(value, list):
        raise _validation(f"{field} must be an array")
    groups: list[dict] = []
    for index, group in enumerate(value):
        group_field = f"{field}[{index}]"
        if not isinstance(group, dict):
            raise _validation(f"{group_field} must be a JSON object")
        unknown = set(group) - {"fixture_id", "steps"}
        if unknown:
            raise _validation(
                f"{group_field} unknown fields: {', '.join(sorted(unknown))}"
            )
        if "fixture_id" not in group:
            raise _validation(f"{group_field}.fixture_id is required")
        fixture_id = group["fixture_id"]
        if not isinstance(fixture_id, str) or fixture_id == "":
            raise _validation(f"{group_field}.fixture_id must be a non-empty string")
        if "steps" not in group:
            raise _validation(f"{group_field}.steps is required")
        steps = _validate_steps(group["steps"], f"{group_field}.steps")
        groups.append({"fixture_id": fixture_id, "steps": steps})
    return groups


def _archive_context_in(value: object, index: int, instance: dict) -> dict:
    """Validate one archived frozen context into the internal context shape.

    The internal context does not carry the frozen case name or parameters
    (those live on the instance), but the archive repeats them, so they are
    checked for one-to-one consistency with the paired run instance.
    """
    field = f"contexts[{index}]"
    if not isinstance(value, dict):
        raise _validation(f"{field} must be a JSON object")
    allowed = {
        "case_name",
        "kind",
        "parameters",
        "timeout_seconds",
        "setup",
        "steps",
        "teardown",
        "resource_requirements",
    }
    unknown = set(value) - allowed
    if unknown:
        raise _validation(
            f"{field} unknown fields: {', '.join(sorted(unknown))}"
        )
    for name in (
        "case_name",
        "kind",
        "parameters",
        "timeout_seconds",
        "setup",
        "steps",
        "teardown",
    ):
        if name not in value:
            raise _validation(f"{field}.{name} is required")

    case_name = value["case_name"]
    if not isinstance(case_name, str) or not case_name.strip():
        raise _validation(f"{field}.case_name must be a non-empty string")
    if case_name != instance["case_name"]:
        raise _validation(
            f"{field}.case_name must match the paired run instance"
        )
    parameters = value["parameters"]
    if not isinstance(parameters, dict):
        raise _validation(f"{field}.parameters must be a JSON object")
    if parameters != instance["parameters"]:
        raise _validation(
            f"{field}.parameters must match the paired run instance"
        )

    kind = value["kind"]
    if not isinstance(kind, str) or kind not in ALLOWED_KINDS:
        raise _validation(f"{field}.kind must be one of: {', '.join(ALLOWED_KINDS)}")
    timeout = value["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise _validation(f"{field}.timeout_seconds must be an integer")
    if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
        raise _validation(
            f"{field}.timeout_seconds must be between "
            f"{MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
        )
    setup = _archive_fixture_groups(value["setup"], f"{field}.setup")
    teardown = _archive_fixture_groups(value["teardown"], f"{field}.teardown")
    steps = _validate_steps(value["steps"], f"{field}.steps")
    if not steps:
        raise _validation(f"{field}.steps must not be empty")
    context = {
        "kind": kind,
        "timeout_seconds": timeout,
        "setup": setup,
        "steps": steps,
        "teardown": teardown,
        "resource_requirements": {},
    }
    if "resource_requirements" in value:
        context["resource_requirements"] = _validate_resource_requirements(
            value["resource_requirements"]
        )
    return context


def _archive_sorted_lines(value: object, field: str, *, allow_empty: bool) -> list[int]:
    """Unique positive integers already normalized into ascending order."""
    if not isinstance(value, list):
        raise _validation(f"{field} must be an array")
    if not allow_empty and not value:
        raise _validation(f"{field} must not be empty")
    lines: list[int] = []
    previous: int | None = None
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise _validation(f"{field}[{index}] must be a positive integer")
        if previous is not None and item <= previous:
            raise _validation(
                f"{field} must contain unique integers in ascending order"
            )
        previous = item
        lines.append(item)
    return lines


def _archive_coverage_in(value: object) -> dict[str, dict[str, set[int]]]:
    """Validate archived normalized coverage into the internal accumulator
    shape ({path: {"executable_lines": set, "covered_lines": set}}).

    The merged archive (unlike per-result fragments) may hold any number of
    files or lines, but every file still obeys the existing line constraints:
    non-empty unique ascending positive executable lines, with covered lines
    a unique ascending subset.
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
    files_value = value["files"]
    if not isinstance(files_value, list):
        raise _validation("coverage.files must be an array")

    accumulated: dict[str, dict[str, set[int]]] = {}
    previous_path: str | None = None
    for index, entry in enumerate(files_value):
        field = f"coverage.files[{index}]"
        if not isinstance(entry, dict):
            raise _validation(f"{field} must be a JSON object")
        entry_unknown = set(entry) - {"path", "executable_lines", "covered_lines"}
        if entry_unknown:
            raise _validation(
                f"{field} unknown fields: {', '.join(sorted(entry_unknown))}"
            )
        for name in ("path", "executable_lines", "covered_lines"):
            if name not in entry:
                raise _validation(f"{field}.{name} is required")
        path = entry["path"]
        if not isinstance(path, str) or path == "":
            raise _validation(f"{field}.path must be a non-empty string")
        if previous_path is not None and not path > previous_path:
            raise _validation(
                "coverage.files must be ordered by path without duplicates"
            )
        previous_path = path
        executable = _archive_sorted_lines(
            entry["executable_lines"],
            f"{field}.executable_lines",
            allow_empty=False,
        )
        covered = _archive_sorted_lines(
            entry["covered_lines"],
            f"{field}.covered_lines",
            allow_empty=True,
        )
        if not set(covered).issubset(executable):
            raise _validation(
                f"{field}.covered_lines must be a subset of executable_lines"
            )
        accumulated[path] = {
            "executable_lines": set(executable),
            "covered_lines": set(covered),
        }
    return accumulated


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
        self._pools: dict[str, dict] = {}
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
            "resource_requirements",
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
        if (
            "resource_requirements" in payload
            and payload["resource_requirements"] is not None
        ):
            case["resource_requirements"] = _validate_resource_requirements(
                payload["resource_requirements"]
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
            for pool_id, amount in case.get("resource_requirements", {}).items():
                pool = self._pools.get(pool_id)
                if pool is None:
                    raise ApiError(
                        404,
                        "resource_pool_not_found",
                        f"resource pool {pool_id!r} not found",
                    )
                if amount > pool["capacity"]:
                    raise _validation(
                        f"resource_requirements.{pool_id} exceeds pool capacity "
                        f"{pool['capacity']}"
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
            requirements = case.get("resource_requirements") or {}
            instances = []
            for index, values in enumerate(parameters):
                instance = {
                    "id": f"{case_id}[{index}]",
                    "parameters": copy.deepcopy(values),
                    "setup": copy.deepcopy(setup),
                    "steps": copy.deepcopy(case["steps"]),
                    "teardown": copy.deepcopy(teardown),
                }
                if requirements:
                    instance["resource_requirements"] = copy.deepcopy(requirements)
                instances.append(instance)
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

    # -- resource pools ----------------------------------------------------

    def _allocated_by_pool(self, now_ms: int) -> dict[str, int]:
        """Live allocation per pool: frozen requirements of every instance
        holding a lease that still occupies resources (unconsumed and either
        unexpired or heartbeat-expired) in any open run."""
        allocated: dict[str, int] = {}
        for run in self._runs.values():
            if run["status"] != "open":
                continue
            for instance, context in zip(run["instances"], run["contexts"]):
                if not self._lease_holds_resources(instance.get("lease"), now_ms):
                    continue
                for pool_id, amount in context.get("resource_requirements", {}).items():
                    allocated[pool_id] = allocated.get(pool_id, 0) + amount
        return allocated

    @staticmethod
    def _pool_view(pool: dict, allocated: Mapping[str, int]) -> dict:
        used = allocated.get(pool["id"], 0)
        return {
            "id": pool["id"],
            "name": pool["name"],
            "capacity": pool["capacity"],
            "allocated": used,
            "available": pool["capacity"] - used,
        }

    def create_resource_pool(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"id", "name", "capacity"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        for field in ("id", "name", "capacity"):
            if field not in payload:
                raise _validation(f"{field} is required")

        pool_id = _clean_text(payload["id"], "id")
        name = _clean_text(payload["name"], "name")
        capacity = payload["capacity"]
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise _validation("capacity must be an integer")
        if not MIN_POOL_CAPACITY <= capacity <= MAX_POOL_CAPACITY:
            raise _validation(
                f"capacity must be between {MIN_POOL_CAPACITY} and {MAX_POOL_CAPACITY}"
            )

        with self._lock:
            if pool_id in self._pools:
                raise ApiError(
                    409,
                    "resource_pool_exists",
                    f"resource pool {pool_id!r} already exists",
                )
            pool = {"id": pool_id, "name": name, "capacity": capacity}
            self._pools[pool_id] = pool
            return self._pool_view(pool, {})

    def get_resource_pool(self, pool_id: str) -> dict:
        with self._lock:
            pool = self._pools.get(pool_id)
            if pool is None:
                raise ApiError(
                    404,
                    "resource_pool_not_found",
                    f"resource pool {pool_id!r} not found",
                )
            return self._pool_view(pool, self._allocated_by_pool(_now_ms()))

    def list_resource_pools(self) -> list[dict]:
        """Pools are returned in creation order with live allocation."""
        with self._lock:
            allocated = self._allocated_by_pool(_now_ms())
            return [self._pool_view(pool, allocated) for pool in self._pools.values()]

    def delete_resource_pool(self, pool_id: str) -> None:
        with self._lock:
            if pool_id not in self._pools:
                raise ApiError(
                    404,
                    "resource_pool_not_found",
                    f"resource pool {pool_id!r} not found",
                )
            for case in self._cases.values():
                if pool_id in case.get("resource_requirements", {}):
                    raise ApiError(
                        409,
                        "resource_pool_in_use",
                        f"resource pool {pool_id!r} is still in use",
                    )
            # Open runs still claim against their frozen requirements;
            # completed runs no longer allocate and never block deletion.
            for run in self._runs.values():
                if run["status"] != "open":
                    continue
                if any(
                    pool_id in context.get("resource_requirements", {})
                    for context in run["contexts"]
                ):
                    raise ApiError(
                        409,
                        "resource_pool_in_use",
                        f"resource pool {pool_id!r} is still in use",
                    )
            del self._pools[pool_id]

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
            "resource_requirements": copy.deepcopy(
                case.get("resource_requirements", {})
            ),
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

    # -- run archives -----------------------------------------------------

    def export_run_archive(self, run_id: str) -> dict:
        """Build the self-contained archive of a completed run.

        Contains only the frozen run record (full report shape), per-instance
        contexts and normalized merged coverage — never catalog objects,
        resource pool definitions, leases or other runs. Read-only and
        deterministic: repeated exports of an unchanged run produce
        byte-identical UTF-8 bodies once the HTTP layer sorts object keys.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] != "completed":
                raise ApiError(
                    409, "run_incomplete", f"run {run_id!r} is not completed"
                )
            contexts = [
                _archive_context_view(instance, context)
                for instance, context in zip(run["instances"], run["contexts"])
            ]
            coverage = {
                "files": _archive_coverage_files(self._coverage.get(run_id, {}))
            }
            return {
                "archive_version": ARCHIVE_VERSION,
                "run": self._run_report(run),
                "contexts": contexts,
                "coverage": coverage,
            }

    def import_run_archive(self, payload: object) -> dict:
        """Restore a completed run from a full self-contained archive.

        Everything is validated against the same shapes the running service
        itself produces (version, field sets, instance/context one-to-one
        correspondence, unique instance ids, recomputable summary, normalized
        coverage, normal-vs-retry lineage consistency) before any state is
        touched, so a failed import never occupies the id or leaves a partial
        run or coverage record.
        """
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"archive_version", "run", "contexts", "coverage"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        for field in ("archive_version", "run", "contexts", "coverage"):
            if field not in payload:
                raise _validation(f"{field} is required")

        version = payload["archive_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise _validation("archive_version must be an integer")
        if version != ARCHIVE_VERSION:
            raise _validation(
                f"unsupported archive_version: expected {ARCHIVE_VERSION}"
            )

        run_id, instances, lineage = self._archive_run_in(payload["run"])
        contexts_value = payload["contexts"]
        if not isinstance(contexts_value, list):
            raise _validation("contexts must be an array")
        if len(contexts_value) != len(instances):
            raise _validation(
                "contexts must contain exactly one entry per run instance"
            )
        contexts = [
            _archive_context_in(value, index, instances[index])
            for index, value in enumerate(contexts_value)
        ]
        coverage_map = _archive_coverage_in(payload["coverage"])

        # Construct the internal record only after full validation; deepcopy
        # isolates stored state from both request and response objects.
        run = {
            "id": run_id,
            "status": "completed",
            "instances": copy.deepcopy(instances),
            "contexts": copy.deepcopy(contexts),
            **lineage,
        }
        coverage: dict[str, dict[str, set[int]]] = {}
        for path, entry in coverage_map.items():
            coverage[path] = {
                "executable_lines": set(entry["executable_lines"]),
                "covered_lines": set(entry["covered_lines"]),
            }

        with self._lock:
            if run_id in self._runs:
                raise ApiError(
                    409, "run_exists", f"run {run_id!r} already exists"
                )
            self._runs[run_id] = run
            if coverage:
                self._coverage[run_id] = coverage
            return self._run_report(run)

    def _archive_run_in(self, value: object) -> tuple[str, list[dict], dict]:
        """Validate the archived run block into (run id, internal instances,
        lineage fields), recomputing the summary from the results."""
        if not isinstance(value, dict):
            raise _validation("run must be a JSON object")
        allowed = {
            "id",
            "status",
            "passed",
            "instances",
            "summary",
            "retry_of",
            "root_run_id",
            "attempt",
        }
        unknown = set(value) - allowed
        if unknown:
            raise _validation(
                f"run unknown fields: {', '.join(sorted(unknown))}"
            )
        for field in ("id", "status", "passed", "instances", "summary"):
            if field not in value:
                raise _validation(f"run.{field} is required")

        run_id = value["id"]
        if not isinstance(run_id, str) or not run_id.strip():
            raise _validation("run.id must be a non-empty string")
        if value["status"] != "completed":
            raise _validation("run.status must be \"completed\"")

        lineage = self._archive_lineage_in(value)

        instances_value = value["instances"]
        if not isinstance(instances_value, list) or not instances_value:
            raise _validation("run.instances must be a non-empty array")
        instances: list[dict] = []
        seen_ids: set[str] = set()
        summary = {
            "total": len(instances_value),
            "pending": 0,
            "passed": 0,
            "failed": 0,
            "error": 0,
            "skipped": 0,
            "duration_ms": 0,
        }
        for index, item in enumerate(instances_value):
            field = f"run.instances[{index}]"
            if not isinstance(item, dict):
                raise _validation(f"{field} must be a JSON object")
            unknown = set(item) - {
                "instance_id",
                "case_id",
                "case_name",
                "parameters",
                "outcome",
                "duration_ms",
                "details",
            }
            if unknown:
                raise _validation(
                    f"{field} unknown fields: {', '.join(sorted(unknown))}"
                )
            for name in (
                "instance_id",
                "case_id",
                "case_name",
                "parameters",
                "outcome",
                "duration_ms",
            ):
                if name not in item:
                    raise _validation(f"{field}.{name} is required")
            instance_id = item["instance_id"]
            if not isinstance(instance_id, str) or instance_id == "":
                raise _validation(f"{field}.instance_id must be a non-empty string")
            case_id = item["case_id"]
            if not isinstance(case_id, str) or case_id == "":
                raise _validation(f"{field}.case_id must be a non-empty string")
            case_name = item["case_name"]
            if not isinstance(case_name, str) or not case_name.strip():
                raise _validation(f"{field}.case_name must be a non-empty string")
            parameters = item["parameters"]
            if not isinstance(parameters, dict):
                raise _validation(f"{field}.parameters must be a JSON object")
            for name, param in parameters.items():
                if not _valid_param_name(name) or not _is_scalar(param):
                    raise _validation(
                        f"{field}.parameters must map valid names to scalar values"
                    )
            outcome = item["outcome"]
            if outcome == "pending" or outcome not in ALLOWED_OUTCOMES:
                raise _validation(
                    f"{field}.outcome must be one of: {', '.join(ALLOWED_OUTCOMES)}"
                )
            duration_ms = item["duration_ms"]
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
                raise _validation(f"{field}.duration_ms must be an integer")
            if duration_ms < 0:
                raise _validation(f"{field}.duration_ms must be non-negative")
            if instance_id in seen_ids:
                raise _validation(
                    f"run.instances must not contain duplicate instance id "
                    f"{instance_id!r}"
                )
            seen_ids.add(instance_id)

            internal = {
                "instance_id": instance_id,
                "case_id": case_id,
                "case_name": case_name,
                "parameters": copy.deepcopy(parameters),
                "outcome": outcome,
                "duration_ms": duration_ms,
            }
            if "details" in item:
                internal["details"] = copy.deepcopy(item["details"])
            instances.append(internal)
            summary[outcome] += 1
            summary["duration_ms"] += duration_ms

        if value["summary"] != summary:
            raise _validation(
                "run.summary is not consistent with the instance results"
            )
        expected_passed = summary["failed"] == 0 and summary["error"] == 0
        if not isinstance(value["passed"], bool) or value["passed"] != expected_passed:
            raise _validation(
                "run.passed is not consistent with the instance results"
            )
        return run_id, instances, lineage

    @staticmethod
    def _archive_lineage_in(value: dict) -> dict:
        """Validate normal-run versus retry-run metadata self-consistency.

        Retry archives keep their frozen lineage even when the ancestor runs
        were never imported: retry_of, root_run_id and attempt must simply be
        present together and individually well-formed, and a normal run must
        carry none of them.
        """
        lineage_fields = ("retry_of", "root_run_id", "attempt")
        present = [name for name in lineage_fields if name in value]
        if present and len(present) != len(lineage_fields):
            raise _validation(
                "run must carry retry_of, root_run_id and attempt together"
            )
        if not present:
            return {}
        retry_of = value["retry_of"]
        root_run_id = value["root_run_id"]
        attempt = value["attempt"]
        for name, ref in (("retry_of", retry_of), ("root_run_id", root_run_id)):
            if not isinstance(ref, str) or not ref.strip():
                raise _validation(f"run.{name} must be a non-empty string")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise _validation("run.attempt must be a positive integer")
        if retry_of == value["id"]:
            raise _validation("run.retry_of must not be the run's own id")
        # A first attempt retries a normal run, so its root run is the direct
        # source; deeper attempts cannot be cross-checked without ancestors.
        if attempt == 1 and root_run_id != retry_of:
            raise _validation(
                "run.root_run_id must equal run.retry_of for the first attempt"
            )
        return {
            "retry_of": retry_of,
            "root_run_id": root_run_id,
            "attempt": attempt,
        }

    # -- leases / claims --------------------------------------------------

    @staticmethod
    def _lease_active(lease: object, now_ms: int) -> bool:
        """A lease is valid until (but not once) current time reaches expiry."""
        return isinstance(lease, dict) and not lease.get("consumed") and lease["expires_at"] > now_ms

    @staticmethod
    def _lease_heartbeat_expired(lease: object, now_ms: int) -> bool:
        """A heartbeat-enabled lease whose heartbeat deadline has been reached.

        Such a lease is stuck: it is no longer valid, but it is not dropped
        either — the instance stays pending and unclaimable and the pool
        allocation stays held until the hang detector reaps it.
        """
        return (
            isinstance(lease, dict)
            and not lease.get("consumed")
            and lease.get("heartbeat_deadline_at") is not None
            and lease["heartbeat_deadline_at"] <= now_ms
        )

    @staticmethod
    def _lease_holds_resources(lease: object, now_ms: int) -> bool:
        """Unconsumed lease still occupying its pool allocation: either still
        valid, or heartbeat-expired (stuck until reaped via hangs)."""
        if not isinstance(lease, dict) or lease.get("consumed"):
            return False
        if lease["expires_at"] > now_ms:
            return True
        deadline = lease.get("heartbeat_deadline_at")
        return deadline is not None and deadline <= now_ms

    def _drop_expired_leases(self, run: dict, now_ms: int) -> None:
        """Expired leases vanish silently; the instances stay pending and
        carry no result, duration or coverage, and become claimable again.
        Heartbeat-expired leases are the exception: they stay attached so
        the instance remains pending, unclaimable and resource-holding."""
        for instance in run["instances"]:
            lease = instance.get("lease")
            if lease is not None and not self._lease_active(lease, now_ms):
                if self._lease_heartbeat_expired(lease, now_ms):
                    continue
                instance.pop("lease", None)

    @staticmethod
    def _claim_view(instance: dict, context: dict, lease: dict) -> dict:
        view = {
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
        if "heartbeat_seconds" in lease:
            view["last_heartbeat_at"] = lease["last_heartbeat_at"]
            view["heartbeat_deadline_at"] = lease["heartbeat_deadline_at"]
        requirements = context.get("resource_requirements")
        if requirements:
            view["resource_requirements"] = copy.deepcopy(requirements)
        return view

    def claim_instances(self, run_id: str, payload: object) -> dict:
        """Lease pending instances of one run to one worker.

        Selection follows frozen order and skips every instance that still
        holds an unexpired, unconsumed lease, plus every instance whose
        frozen resource requirements do not currently fit (later instances
        that do fit are still leased). The whole scan-and-lease step
        runs under the service lock, so concurrent claims can never attach
        two active leases to the same instance nor over-allocate a pool.
        """
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"worker_id", "max_items", "lease_seconds", "heartbeat_seconds"}
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

        heartbeat_seconds: int | None = None
        if "heartbeat_seconds" in payload:
            value = payload["heartbeat_seconds"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise _validation("heartbeat_seconds must be an integer")
            if not MIN_HEARTBEAT_SECONDS <= value <= MAX_HEARTBEAT_SECONDS:
                raise _validation(
                    f"heartbeat_seconds must be between "
                    f"{MIN_HEARTBEAT_SECONDS} and {MAX_HEARTBEAT_SECONDS}"
                )
            if value >= lease_seconds:
                raise _validation("heartbeat_seconds must be less than lease_seconds")
            heartbeat_seconds = value

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
            # Live allocation across every open run; the whole scan-and-lease
            # step holds the service lock, so concurrent claims can never
            # push a pool's allocated total above its capacity.
            allocated = self._allocated_by_pool(now_ms)

            claims: list[dict] = []
            for instance, context in zip(run["instances"], run["contexts"]):
                if len(claims) >= max_items:
                    break
                if instance["outcome"] != "pending" or "lease" in instance:
                    continue
                requirements = context.get("resource_requirements") or {}
                # An instance whose frozen requirements cannot be satisfied
                # right now is skipped without blocking later instances.
                if not self._requirements_fit(requirements, allocated):
                    continue
                claim_id = secrets.token_urlsafe(18)
                lease = {
                    "claim_id": claim_id,
                    "worker_id": worker_id,
                    "claimed_at": now_ms,
                    "expires_at": expires_at,
                    "consumed": False,
                }
                if heartbeat_seconds is not None:
                    # Liveness tracking: the first heartbeat is due one
                    # interval after claiming, never past the lease expiry.
                    lease["heartbeat_seconds"] = heartbeat_seconds
                    lease["last_heartbeat_at"] = now_ms
                    lease["heartbeat_deadline_at"] = min(
                        now_ms + heartbeat_seconds * 1000, expires_at
                    )
                instance["lease"] = lease
                for pool_id, amount in requirements.items():
                    allocated[pool_id] = allocated.get(pool_id, 0) + amount
                claims.append(self._claim_view(instance, context, lease))
            return {"claims": claims}

    def _requirements_fit(
        self, requirements: Mapping[str, int], allocated: Mapping[str, int]
    ) -> bool:
        """True when every required pool can still cover the extra amount."""
        for pool_id, amount in requirements.items():
            pool = self._pools.get(pool_id)
            # Pools cannot be deleted while an open run references them, so a
            # missing pool can only come from a retry of a completed run; it
            # is treated as unsatisfiable rather than an error.
            if pool is None:
                return False
            if allocated.get(pool_id, 0) + amount > pool["capacity"]:
                return False
        return True

    def heartbeat(self, run_id: str, payload: object) -> dict:
        """Refresh the liveness marker of one heartbeat-enabled claim.

        Only the claim's own worker may heartbeat, only while the lease is
        still valid and the previous heartbeat deadline has not been
        reached. A successful heartbeat moves ``last_heartbeat_at`` to now
        and pushes ``heartbeat_deadline_at`` one original interval out
        (never past ``expires_at``); the lease expiry and the frozen hard
        timeout are never extended. The whole check-and-update step runs
        under the service lock.
        """
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"claim_id", "worker_id"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        for field in ("claim_id", "worker_id"):
            if field not in payload:
                raise _validation(f"{field} is required")
        claim_id = _reference(payload["claim_id"], "claim_id")
        worker_id = _clean_text(payload["worker_id"], "worker_id")

        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ApiError(404, "run_not_found", f"run {run_id!r} not found")
            if run["status"] == "completed":
                raise ApiError(
                    409, "run_completed", f"run {run_id!r} is already completed"
                )
            now_ms = _now_ms()
            # Normally expired leases vanish exactly as in claims/results;
            # heartbeat-expired ones stay attached so they can be reported.
            self._drop_expired_leases(run, now_ms)
            target_instance: dict | None = None
            target_lease: dict | None = None
            for instance in run["instances"]:
                lease = instance.get("lease")
                if isinstance(lease, dict) and lease["claim_id"] == claim_id:
                    target_instance = instance
                    target_lease = lease
                    break
            if target_lease is None or target_instance is None:
                raise ApiError(
                    409,
                    "claim_not_active",
                    f"claim {claim_id!r} is not active in run {run_id!r}",
                )
            if target_lease["worker_id"] != worker_id:
                raise ApiError(
                    409,
                    "claim_conflict",
                    f"claim {claim_id!r} is held by another worker",
                )
            if "heartbeat_seconds" not in target_lease:
                raise ApiError(
                    409,
                    "heartbeat_not_enabled",
                    f"claim {claim_id!r} was leased without heartbeats",
                )
            if now_ms >= target_lease["heartbeat_deadline_at"]:
                raise ApiError(
                    409,
                    "heartbeat_expired",
                    f"claim {claim_id!r} has missed its heartbeat deadline",
                )
            target_lease["last_heartbeat_at"] = now_ms
            target_lease["heartbeat_deadline_at"] = min(
                now_ms + target_lease["heartbeat_seconds"] * 1000,
                target_lease["expires_at"],
            )
            return {
                "claim_id": target_lease["claim_id"],
                "instance_id": copy.deepcopy(target_instance["instance_id"]),
                "last_heartbeat_at": target_lease["last_heartbeat_at"],
                "heartbeat_deadline_at": target_lease["heartbeat_deadline_at"],
                "expires_at": target_lease["expires_at"],
            }

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
                # A heartbeat-expired lease is stuck on the instance: its own
                # claim id can no longer submit, only the hang detector reaps.
                if (
                    has_claim
                    and claim_id == lease["claim_id"]
                    and self._lease_heartbeat_expired(lease, now_ms)
                ):
                    raise ApiError(
                        409,
                        "heartbeat_expired",
                        f"claim {claim_id!r} has missed its heartbeat deadline",
                    )
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
        """Detect leased instances whose frozen timeout has elapsed.

        Only pending instances holding a valid lease (unconsumed and not yet
        expired by ``expires_at``) are considered, and only once the server
        clock reaches ``claimed_at`` plus the frozen ``timeout_seconds``.
        Each hit consumes the lease and writes an ``error`` result
        atomically, so a concurrent result submission for the same instance
        can no longer succeed. Instances with a result, no lease, a consumed
        lease or an expired lease are left untouched and keep their
        re-claim semantics. The run is never auto-completed.
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
            # Expired leases vanish silently exactly as in claims/results;
            # their instances stay pending and claimable, never timed out.
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
                # Consume the lease and write the timeout result atomically;
                # a late submission carrying the old claim id then fails
                # with claim_not_active and cannot overwrite the result.
                lease["consumed"] = True
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

    def check_hangs(self, run_id: str, payload: object) -> dict:
        """Detect leased instances whose heartbeat deadline has elapsed.

        Only pending instances holding a heartbeat-enabled lease whose
        ``heartbeat_deadline_at`` has been reached are considered. Each hit
        consumes the lease and writes an ``error`` result atomically, so a
        concurrent heartbeat or result submission for the same claim can no
        longer succeed; the pool allocation is released with the lease. When
        the frozen hard timeout has elapsed at the same time, the existing
        timeout result is written instead and the instance is not listed as
        hung. The run is never auto-completed.
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
            # Expired leases vanish silently exactly as in claims/results;
            # heartbeat-expired ones survive the sweep and are reaped below.
            self._drop_expired_leases(run, now_ms)

            hung: list[str] = []
            for instance, context in zip(run["instances"], run["contexts"]):
                if instance["outcome"] != "pending":
                    continue
                lease = instance.get("lease")
                if lease is None:
                    continue
                if not self._lease_heartbeat_expired(lease, now_ms):
                    continue
                timeout_seconds = context["timeout_seconds"]
                timeout_ms = timeout_seconds * 1000
                deadline_at = lease["claimed_at"] + timeout_ms
                # Consume the lease and write the result atomically, exactly
                # as the timeout detector does; a late heartbeat or submit
                # carrying the old claim id then fails and cannot overwrite.
                lease["consumed"] = True
                instance.pop("lease", None)
                instance["outcome"] = "error"
                if now_ms >= deadline_at:
                    # The hard timeout takes precedence: same result shape as
                    # check_timeouts, and the instance is not listed as hung.
                    instance["duration_ms"] = timeout_ms
                    instance["details"] = {
                        "code": "timeout",
                        "timeout_seconds": timeout_seconds,
                        "worker_id": lease["worker_id"],
                        "claimed_at": lease["claimed_at"],
                        "deadline_at": deadline_at,
                        "detected_at": now_ms,
                    }
                    continue
                instance["duration_ms"] = max(0, now_ms - lease["claimed_at"])
                instance["details"] = {
                    "code": "hung",
                    "worker_id": lease["worker_id"],
                    "claimed_at": lease["claimed_at"],
                    "last_heartbeat_at": lease["last_heartbeat_at"],
                    "heartbeat_deadline_at": lease["heartbeat_deadline_at"],
                    "detected_at": now_ms,
                }
                hung.append(instance["instance_id"])
            return {"hung": hung, "run": self._run_report(run)}

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

    # -- instance stability -------------------------------------------------

    def stability_report(self, payload: object) -> dict:
        """Read-only per-instance stability analysis over completed runs.

        Uses only the frozen run records (instances and final results); the
        catalog is never consulted and nothing is mutated, so repeated calls
        with the same input return identical content and array order.
        """
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"run_ids"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        if "run_ids" not in payload:
            raise _validation("run_ids is required")
        run_ids = _validate_references(payload["run_ids"], "run_ids")
        if not MIN_STABILITY_RUNS <= len(run_ids) <= MAX_STABILITY_RUNS:
            raise _validation(
                f"run_ids must contain between {MIN_STABILITY_RUNS} and "
                f"{MAX_STABILITY_RUNS} entries"
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

            # Instances are keyed by (case_id, instance_id) and ordered by
            # first appearance while scanning the runs in request order; a
            # run that lacks the instance simply adds no observation.
            order: list[tuple[str, str]] = []
            entries: dict[tuple[str, str], dict] = {}
            for run in runs:
                for instance in run["instances"]:
                    key = (instance["case_id"], instance["instance_id"])
                    entry = entries.get(key)
                    if entry is None:
                        entry = entries[key] = {
                            "case_id": instance["case_id"],
                            "instance_id": instance["instance_id"],
                            "case_name": instance["case_name"],
                            "summary": {
                                "total": 0,
                                "effective": 0,
                                "passed": 0,
                                "failed": 0,
                                "error": 0,
                                "skipped": 0,
                                "duration_ms": 0,
                            },
                            "observations": [],
                        }
                        order.append(key)
                    # The frozen name of the most recent observation wins.
                    entry["case_name"] = instance["case_name"]
                    observation = {
                        "run_id": run["id"],
                        "outcome": instance["outcome"],
                        "duration_ms": instance["duration_ms"],
                    }
                    if "retry_of" in run:
                        observation["retry_of"] = run["retry_of"]
                        observation["root_run_id"] = run["root_run_id"]
                        observation["attempt"] = run["attempt"]
                    entry["observations"].append(observation)

                    summary = entry["summary"]
                    outcome = instance["outcome"]
                    summary["total"] += 1
                    summary[outcome] += 1
                    summary["duration_ms"] += instance["duration_ms"]
                    if outcome != "skipped":
                        summary["effective"] += 1

            status_counts = {
                "stable_pass": 0,
                "stable_fail": 0,
                "flaky": 0,
                "insufficient": 0,
            }
            instances: list[dict] = []
            for key in order:
                entry = entries[key]
                summary = entry["summary"]
                effective = summary["effective"]
                if effective < 2:
                    status = "insufficient"
                elif summary["passed"] == effective:
                    status = "stable_pass"
                elif summary["failed"] + summary["error"] == effective:
                    status = "stable_fail"
                else:
                    status = "flaky"
                status_counts[status] += 1
                instances.append(
                    {
                        "case_id": entry["case_id"],
                        "instance_id": entry["instance_id"],
                        "case_name": entry["case_name"],
                        "status": status,
                        "pass_rate": (
                            _rounded_rate(summary["passed"], effective)
                            if effective
                            else None
                        ),
                        "summary": summary,
                        "observations": entry["observations"],
                    }
                )
            return {
                "run_count": len(runs),
                "summary": {"total": len(instances), **status_counts},
                "instances": instances,
            }

    # -- CI gates ----------------------------------------------------------

    def evaluate_ci_gate(self, payload: object) -> dict:
        """Read-only policy evaluation over completed runs.

        Only the explicit policy checks run; failures/errors and coverage
        come from the current (last) run while flakiness is computed across
        all requested runs. Uses only the frozen run records and merged
        coverage; nothing is mutated, so identical input yields identical
        output and order.
        """
        if not isinstance(payload, dict):
            raise _validation("request body must be a JSON object")
        unknown = set(payload) - {"run_ids", "policy"}
        if unknown:
            raise _validation(f"unknown fields: {', '.join(sorted(unknown))}")
        if "run_ids" not in payload:
            raise _validation("run_ids is required")
        if "policy" not in payload:
            raise _validation("policy is required")

        run_ids = _validate_references(payload["run_ids"], "run_ids")
        if not 1 <= len(run_ids) <= MAX_AGGREGATE_RUNS:
            raise _validation(
                f"run_ids must contain between 1 and {MAX_AGGREGATE_RUNS} entries"
            )

        policy_value = payload["policy"]
        if not isinstance(policy_value, dict) or not policy_value:
            raise _validation("policy must be a non-empty JSON object")
        policy_unknown = set(policy_value) - set(GATE_POLICY_FIELDS)
        if policy_unknown:
            raise _validation(
                f"policy unknown fields: {', '.join(sorted(policy_unknown))}"
            )
        limits: dict[str, object] = {}
        for name in GATE_INTEGER_FIELDS:
            if name in policy_value:
                value = policy_value[name]
                # Booleans are numbers in Python but are explicitly not counts.
                if isinstance(value, bool) or not isinstance(value, int):
                    raise _validation(f"policy.{name} must be a non-negative integer")
                if value < 0:
                    raise _validation(f"policy.{name} must be a non-negative integer")
                limits[name] = value
        if "min_coverage_percent" in policy_value:
            value = policy_value["min_coverage_percent"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _validation(
                    "policy.min_coverage_percent must be a finite number between 0 and 100"
                )
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise _validation(
                    "policy.min_coverage_percent must be a finite number between 0 and 100"
                )
            limits["min_coverage_percent"] = value
        if "max_flaky" in limits and len(run_ids) < MIN_GATE_FLAKY_RUNS:
            raise _validation(
                "policy.max_flaky requires at least two runs"
            )

        with self._lock:
            # Structural validation precedes any state lookup; runs are then
            # checked strictly in request order so the first missing run is
            # 404 and the first incomplete run is 409, with no partial result.
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

            current = runs[-1]
            current_report = self._run_report(current)
            checks: list[dict] = []
            if "max_failed" in limits:
                actual = current_report["summary"]["failed"]
                limit = limits["max_failed"]
                checks.append(
                    {
                        "name": "max_failed",
                        "passed": actual <= limit,
                        "actual": actual,
                        "limit": limit,
                    }
                )
            if "max_error" in limits:
                actual = current_report["summary"]["error"]
                limit = limits["max_error"]
                checks.append(
                    {
                        "name": "max_error",
                        "passed": actual <= limit,
                        "actual": actual,
                        "limit": limit,
                    }
                )
            if "min_coverage_percent" in limits:
                limit = limits["min_coverage_percent"]
                accumulated = self._coverage.get(current["id"], {})
                total_executable = sum(
                    len(entry["executable_lines"]) for entry in accumulated.values()
                )
                if total_executable == 0:
                    # No executable lines: coverage is undefined and the
                    # minimum-coverage gate cannot pass.
                    actual: float | None = None
                    passed = False
                else:
                    total_covered = sum(
                        len(entry["covered_lines"]) for entry in accumulated.values()
                    )
                    actual = _coverage_percent(total_covered, total_executable)
                    passed = actual >= limit
                checks.append(
                    {
                        "name": "min_coverage_percent",
                        "passed": passed,
                        "actual": actual,
                        "limit": limit,
                    }
                )
            if "max_flaky" in limits:
                limit = limits["max_flaky"]
                # Flaky instances are keyed by (case_id, instance_id); skipped
                # observations are ignored and runs missing an instance add no
                # observation at all.
                observations: dict[tuple[str, str], set[str]] = {}
                for run in runs:
                    for instance in run["instances"]:
                        outcome = instance["outcome"]
                        if outcome == "skipped":
                            continue
                        key = (instance["case_id"], instance["instance_id"])
                        observations.setdefault(key, set()).add(outcome)
                flaky = sum(
                    1
                    for outcomes in observations.values()
                    if len(outcomes) >= MIN_GATE_FLAKY_RUNS
                    and "passed" in outcomes
                    and ({"failed", "error"} & outcomes)
                )
                checks.append(
                    {
                        "name": "max_flaky",
                        "passed": flaky <= limit,
                        "actual": flaky,
                        "limit": limit,
                    }
                )

            return {
                "current_run_id": current["id"],
                "run_count": len(runs),
                "passed": all(check["passed"] for check in checks),
                "checks": checks,
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
