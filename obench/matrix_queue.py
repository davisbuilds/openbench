#!/usr/bin/env python3
"""Queue-based benchmark runner with retry budgets, arm pausing, and coverage.

Usage::

    obench matrix --spec experiments/specs/laguna-inkling.toml

The spec is a TOML file declaring arms (harness|harness-pack x model), task
groups, retry budgets, and run configuration.  The queue manager:

1.  Enumerates all planned cells (arm x task x trial).
2.  For each cell, checks a results JSONL for a SATISFIED row (failure_class
    NOT in the excluded-from-solve-rate set).  Cells whose only rows are
    excluded-class are RE-QUEUED against their retry budget.
3.  Applies exponential backoff for rate-limited retries.
4.  Pauses an ARM after N consecutive excluded results, revisiting at the end.
5.  Reports per-arm COVERAGE (satisfied / planned) and lists exhausted cells.

Persistent queue-state.json enables exact resume after kill or host restart.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path
from typing import Any

from . import run as bench_run
from . import failure_class as fc_mod
from .paths import PACKAGE_DIR, SOURCE_ROOT, default_results_path, default_tasks_dir

HERE = PACKAGE_DIR
REPO = SOURCE_ROOT

DEFAULT_RUNNER = os.path.join(HERE, "run.py")
DEFAULT_MAX_CONSECUTIVE_EXCLUDED = 5
POLL_INTERVAL_S = 2.0

# Retry budgets: how many times a cell with a given failure class is re-queued.
DEFAULT_RETRY: dict[str, int] = {
    "infra": 2,
    "stall": 1,
    "rate_limited": 3,
}
DEFAULT_RATE_LIMITED_BACKOFF_START_S = 60
DEFAULT_STALL_TIMEOUT = 600


class SpecError(ValueError):
    """Raised when the TOML spec is invalid or incomplete."""


def load_spec(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Parse a TOML spec file into a dict.

    Supports stdlib tomllib (Python 3.11+) and the ``toml`` backport.
    """
    import tomllib
    with open(path, "rb") as fh:
        spec = tomllib.load(fh)
    _validate_spec(spec, str(path))
    return spec


def _validate_spec(spec: dict[str, Any], source: str) -> None:
    """Raise SpecError for missing or invalid fields."""
    arms = spec.get("arm") or spec.get("arms")
    if not arms:
        raise SpecError(f"{source}: at least one [[arm]] is required")
    for i, arm in enumerate(arms):
        if not arm.get("harness"):
            raise SpecError(f"{source}: arm[{i}] missing 'harness'")
        if not arm.get("model"):
            raise SpecError(f"{source}: arm[{i}] missing 'model'")

    task_groups = spec.get("task_group") or spec.get("task_groups") or []
    if not task_groups:
        raise SpecError(f"{source}: at least one [[task_group]] is required")
    for i, tg in enumerate(task_groups):
        if not tg.get("tasks"):
            raise SpecError(f"{source}: task_group[{i}] missing 'tasks'")

    if not spec.get("results_path"):
        raise SpecError(f"{source}: 'results_path' is required")


def enumerate_arms(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Return list of arm dicts from the spec."""
    return spec.get("arm") or spec.get("arms", [])


def enumerate_task_groups(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Return list of task group dicts from the spec."""
    return spec.get("task_group") or spec.get("task_groups", [])


def resolve_tasks(spec: dict[str, Any], base_dir: str = "") -> list[str]:
    """Resolve task names from all task groups into a flat, deduplicated list."""
    seen: set[str] = set()
    tasks: list[str] = []
    for tg in enumerate_task_groups(spec):
        tg_dir = tg.get("tasks_dir", "")
        for name in tg.get("tasks", []):
            full = os.path.join(tg_dir, name) if tg_dir else name
            if full not in seen:
                seen.add(full)
                tasks.append(full)
    return tasks


def resolve_tasks_dir(spec: dict[str, Any], spec_dir: str, cwd: str) -> str:
    """Resolve the effective tasks directory from spec or defaults."""
    spec_tasks_dir = spec.get("tasks_dir", "")
    if spec_tasks_dir:
        return os.path.abspath(os.path.join(spec_dir, spec_tasks_dir))
    try:
        return bench_run.resolve_tasks_dir(None)
    except Exception:
        return os.path.abspath(os.path.join(cwd, "tasks"))


def expand_cells(
    arms: list[dict[str, str]],
    tasks: list[str],
    trials: int,
) -> list[dict[str, Any]]:
    """Enumerate every (arm, task, trial) cell with a stable run_id."""
    cells: list[dict[str, Any]] = []
    for arm in arms:
        harness = arm["harness"]
        model = arm["model"]
        for task in tasks:
            for trial_num in range(1, trials + 1):
                run_id = bench_run.make_run_id(harness, task, model, trial_num)
                cells.append({
                    "arm": f"{harness} x {model}",
                    "arm_idx": arms.index(arm),
                    "harness": harness,
                    "model": model,
                    "task": task,
                    "trial": trial_num,
                    "run_id": run_id,
                })
    return cells


# ── Queue state ─────────────────────────────────────────────────────────

class QueueState:
    """Persistent queue state for resume-safe run management.

    Written to a JSON file after each cell completes so a killed or restarted
    ``obench matrix`` invocation resumes exactly where it left off.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding="utf-8")
                return json.loads(raw) if raw.strip() else {}
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.rename(self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, mapping: dict[str, Any]) -> None:
        self._data.update(mapping)

    @property
    def data(self) -> dict[str, Any]:
        return self._data


# ── Retry and arm-state tracking ────────────────────────────────────────

class ArmState:
    """Per-arm retry and pause tracking."""

    def __init__(self, name: str, retry_budgets: dict[str, int] | None = None) -> None:
        self.name = name
        self.budgets: dict[str, int] = dict(retry_budgets or DEFAULT_RETRY)
        self.consecutive_excluded = 0
        self.paused = False
        self.satisfied = 0
        self.planned = 0
        self.exhausted_cells: list[str] = []

    def retry_budget(self, failure_class: str | None) -> int:
        """Re-queues allowed for a cell that failed with ``failure_class``.

        This governs RETRIES only. A cell with no prior row has no failure class
        and is not retrying, so callers must let the first attempt through
        without consulting this budget (see ``should_exhaust``).
        """
        if failure_class is None:
            return 0
        return self.budgets.get(failure_class, 0)

    def should_exhaust(self, failure_class: str | None, attempts: int) -> bool:
        """True when a cell has used up its retries for this failure class.

        ``attempts == 0`` is the first run of the cell, never an exhaustion:
        charging it against a retry budget marked every cell EXHAUSTED before
        it ever executed (observed on first live use: 0/9 satisfied).
        """
        if attempts == 0:
            return False
        return attempts >= self.retry_budget(failure_class)

    def record_excluded(self) -> None:
        self.consecutive_excluded += 1

    def record_included(self) -> None:
        self.consecutive_excluded = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "budgets": dict(self.budgets),
            "consecutive_excluded": self.consecutive_excluded,
            "paused": self.paused,
            "satisfied": self.satisfied,
            "planned": self.planned,
            "exhausted_cells": list(self.exhausted_cells),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArmState":
        self = cls(d["name"], d.get("budgets"))
        self.consecutive_excluded = d.get("consecutive_excluded", 0)
        self.paused = d.get("paused", False)
        self.satisfied = d.get("satisfied", 0)
        self.planned = d.get("planned", 0)
        self.exhausted_cells = list(d.get("exhausted_cells", []))
        return self


# ── Cell satisfaction check ─────────────────────────────────────────────

def load_results_ids(results_path: str) -> dict[str, dict[str, Any]]:
    """Load results JSONL into {run_id: row} mapping (last row wins per id)."""
    rows: dict[str, dict[str, Any]] = {}
    if not os.path.isfile(results_path):
        return rows
    with open(results_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            rid = row.get("run_id")
            if rid:
                rows[rid] = row
    return rows


def cell_is_satisfied(row: dict[str, Any] | None) -> bool:
    """A cell is SATISFIED when it has a row whose failure_class is NOT excluded."""
    if row is None:
        return False
    fc = row.get("failure_class") or fc_mod.class_for_report(row)
    return fc not in fc_mod.EXCLUDED_FROM_SOLVE_RATE


def row_failure_class(row: dict[str, Any] | None) -> str | None:
    """Return the failure class for a row, or None if no row."""
    if row is None:
        return None
    fc = row.get("failure_class")
    if fc in fc_mod.FAILURE_CLASSES:
        return fc
    return None


# ── Backoff ──────────────────────────────────────────────────────────────

def backoff_for_failure(fc: str | None, attempt: int,
                        base_s: float = DEFAULT_RATE_LIMITED_BACKOFF_START_S) -> float:
    """Return the backoff delay in seconds before re-queueing a cell.

    Only rate_limited gets exponential backoff.  Other failure classes wait
    a fixed 10s to avoid hammering the harness.
    """
    if fc == "rate_limited":
        return base_s * (2 ** (attempt - 1))
    return 10.0


# ── Queue execution ─────────────────────────────────────────────────────

def build_runner_command(
    cell: dict[str, Any],
    results_path: str,
    tasks_dir: str,
    timeout: int,
    stall_timeout: int | None,
    exec_mode: str,
) -> list[str]:
    """Build the ``obench run`` subprocess argv for one cell."""
    cmd = [sys.executable, DEFAULT_RUNNER]
    cmd.extend([
        "--force",  # always re-run even if run_id exists (prior excluded rows)
        "--harness", cell["harness"],
        "--model", cell["model"],
        "--task", cell["task"],
        "--trial", str(cell["trial"]),
        "--timeout", str(timeout),
        "--results-path", results_path,
        "--tasks-dir", tasks_dir,
    ])
    if exec_mode == "docker":
        cmd.extend(["--exec", "docker"])
    if stall_timeout is not None:
        cmd.extend(["--stall-timeout", str(stall_timeout)])
    # Enable proxy for stall-kill support (required for stall-timeout to work)
    if stall_timeout is not None:
        cmd.append("--proxy")
    return cmd


def run_runner(cmd: list[str], timeout_s: float | None = None) -> int:
    """Run one obench runner invocation and return its exit code."""
    proc = subprocess.Popen(cmd, start_new_session=True)
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 15)  # SIGTERM
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, 9)  # SIGKILL
            except ProcessLookupError:
                pass
            proc.wait()
        return -1


# ── Main ────────────────────────────────────────────────────────────────

def run_matrix(spec: dict[str, Any], spec_dir: str, cwd: str) -> int:
    """Execute the full matrix queue and return exit code (0 = full coverage)."""
    results_path = os.path.abspath(os.path.join(spec_dir, spec.get("results_path", "results.jsonl")))
    timeout = spec.get("timeout", 2400)
    exec_mode = spec.get("exec_mode", "local")
    trials = spec.get("trials", 1)
    stall_timeout = spec.get("stall_timeout") or (
        int(os.environ.get("OPENBENCH_STALL_TIMEOUT", "0")) or None
    )
    if stall_timeout is None and spec.get("proxy", False):
        stall_timeout = DEFAULT_STALL_TIMEOUT

    retry_budgets = dict(DEFAULT_RETRY)
    retry_cfg = spec.get("retry", {})
    if isinstance(retry_cfg, dict):
        for fc in ("infra", "stall", "rate_limited"):
            if fc in retry_cfg:
                retry_budgets[fc] = int(retry_cfg[fc])
    rate_limited_backoff = float(
        retry_cfg.get("rate_limited_backoff_start", DEFAULT_RATE_LIMITED_BACKOFF_START_S)
        if isinstance(retry_cfg, dict) else DEFAULT_RATE_LIMITED_BACKOFF_START_S
    )
    max_consecutive_excluded = int(
        spec.get("max_consecutive_excluded", DEFAULT_MAX_CONSECUTIVE_EXCLUDED)
    )

    tasks_dir = resolve_tasks_dir(spec, spec_dir, cwd)
    tasks = resolve_tasks(spec, spec_dir)
    arms = enumerate_arms(spec)
    all_cells = expand_cells(arms, tasks, trials)

    # Quick sanity check
    missing = [t for t in tasks if not os.path.isfile(os.path.join(tasks_dir, t.split("/")[0], "instruction.md"))
               and not os.path.isfile(os.path.join(tasks_dir, t, "instruction.md"))]
    if missing:
        print(
            f"ERROR: {len(missing)} task(s) not found under {tasks_dir}: "
            + ", ".join(missing[:5]),
            file=sys.stderr,
        )
        return 1

    # ── Initialize queue state ──────────────────────────────────────────
    qdir = spec.get("ledger_dir") or os.path.join(
        os.path.dirname(results_path), ".matrix-queue")
    queue_state_path = os.path.join(str(qdir), "queue-state.json")
    os.makedirs(str(qdir), exist_ok=True)
    state = QueueState(queue_state_path)

    # Per-arm state (restored or fresh)
    arm_states_raw = state.get("arm_states", {})
    arm_states: dict[str, ArmState] = {}
    for arm in arms:
        name = f"{arm['harness']} x {arm['model']}"
        if name in arm_states_raw:
            arm_states[name] = ArmState.from_dict(arm_states_raw[name])
        else:
            as_ = ArmState(name, retry_budgets)
            as_.planned = sum(1 for c in all_cells if c["arm"] == name)
            arm_states[name] = as_

    # Pending cell queue: list of (arm_name, cell) tuples.
    # Restore from saved state if available.
    pending_raw = state.get("pending", [])
    if pending_raw:
        pending: list[tuple[str, int, dict[str, Any]]] = [
            (p[0], p[1], next(c for c in all_cells if c["run_id"] == p[2]))
            for p in pending_raw
        ]
    else:
        pending = [(c["arm"], c["arm_idx"], c) for c in all_cells]

    # Track retry counts per run_id
    retry_counts: dict[str, int] = dict(state.get("retry_counts", {}))

    paused_arms: list[tuple[str, int, dict[str, Any]]] = []
    visit_paused = False

    print(
        f"Matrix queue: {len(all_cells)} planned cells, "
        f"{len(arms)} arm(s), {len(tasks)} task(s), {trials} trial(s)"
    )

    while pending or (paused_arms and not visit_paused):
        # If we exhausted the main queue and have paused arms, revisit them.
        if not pending and paused_arms and not visit_paused:
            print(f"\n-- Revisiting {len(paused_arms)} paused arm(s) --")
            pending = list(paused_arms)
            paused_arms = []
            visit_paused = True

        arm_name, arm_idx, cell = pending.pop(0)
        as_ = arm_states[arm_name]
        run_id = cell["run_id"]

        # Check if cell is already satisfied
        existing = load_results_ids(results_path)
        row = existing.get(run_id)
        if cell_is_satisfied(row):
            as_.satisfied += 1
            print(f"    SATISFIED {run_id} (coverage {as_.satisfied}/{as_.planned})")
            state.set("arm_states", {n: a.to_dict() for n, a in arm_states.items()})
            state.save()
            continue

        # Check retry budget
        attempt = retry_counts.get(run_id, 0)
        fc = row_failure_class(row)
        budget = as_.retry_budget(fc)
        if as_.should_exhaust(fc, attempt):
            as_.exhausted_cells.append(run_id)
            print(f"    EXHAUSTED {run_id} (fc={fc} attempts={attempt} budget={budget})")
            state.set("arm_states", {n: a.to_dict() for n, a in arm_states.items()})
            state.save()
            continue

        # Backoff if re-trying
        if attempt > 0:
            delay = backoff_for_failure(fc, attempt, rate_limited_backoff)
            print(f"    BACKOFF {run_id} attempt={attempt}/{budget} fc={fc} wait={delay:.0f}s")
            time.sleep(delay)

        # Run the cell
        print(f"    RUN    {run_id}", flush=True)
        cmd = build_runner_command(
            cell, results_path, tasks_dir, timeout, stall_timeout, exec_mode)
        rc = run_runner(cmd, timeout + 60)

        if rc != 0:
            print(f"    WARN runner exit={rc} for {run_id}", file=sys.stderr)

        # Re-check satisfaction
        existing = load_results_ids(results_path)
        row = existing.get(run_id)
        if cell_is_satisfied(row):
            as_.satisfied += 1
            as_.record_included()
            print(f"    SATISFIED {run_id} (coverage {as_.satisfied}/{as_.planned})")
        else:
            new_fc = row_failure_class(row)
            retry_counts[run_id] = retry_counts.get(run_id, 0) + 1
            if new_fc is not None and new_fc in fc_mod.EXCLUDED_FROM_SOLVE_RATE:
                as_.record_excluded()
                # Re-queue if budget allows
                budget_remaining = as_.retry_budget(new_fc) - retry_counts.get(run_id, 0)
                if budget_remaining > 0:
                    pending.insert(0, (arm_name, arm_idx, cell))
                    print(f"    RE-QUEUED {run_id} (fc={new_fc} retry={budget_remaining} left)")
                else:
                    as_.exhausted_cells.append(run_id)
                    print(f"    EXHAUSTED {run_id} (fc={new_fc} retry budget exhausted)")

        # Arm pause check
        if as_.consecutive_excluded >= max_consecutive_excluded:
            if not as_.paused:
                as_.paused = True
                # Move all remaining cells for this arm to paused list
                arm_cells = [(n, i, c) for n, i, c in pending if n == arm_name]
                pending = [(n, i, c) for n, i, c in pending if n != arm_name]
                paused_arms.extend(arm_cells)
                print(f"    PAUSED arm={arm_name} after {as_.consecutive_excluded} consecutive excluded")

        # Persist state
        state.set("arm_states", {n: a.to_dict() for n, a in arm_states.items()})
        state.set("retry_counts", retry_counts)
        remaining_pending = []
        for n, i, c in pending:
            remaining_pending.append([n, i, c["run_id"]])
        state.set("pending", remaining_pending)
        state.save()

    # ── Final summary ───────────────────────────────────────────────────
    total_planned = len(all_cells)
    total_satisfied = sum(a.satisfied for a in arm_states.values())
    exhausted = [(n, a.exhausted_cells) for n, a in arm_states.items() if a.exhausted_cells]
    paused_final = [n for n, a in arm_states.items() if a.paused]

    print("\n" + "=" * 60)
    print("MATRIX COVERAGE REPORT")
    print("=" * 60)
    for arm in arms:
        name = f"{arm['harness']} x {arm['model']}"
        as_ = arm_states[name]
        pct = (as_.satisfied / as_.planned * 100) if as_.planned > 0 else 0
        marker = " [PAUSED]" if as_.paused else ""
        exhausted_count = len(as_.exhausted_cells)
        exhausted_mark = f" {exhausted_count} exhausted" if exhausted_count else ""
        print(f"  {name}: {as_.satisfied}/{as_.planned} ({pct:.1f}%){marker}{exhausted_mark}")

    if exhausted:
        print("\nExhausted cells (retry budget depleted):")
        for arm_name, cells in exhausted:
            for c in cells:
                print(f"  {arm_name}: {c}")

    failed_arms = [n for n, a in arm_states.items() if a.exhausted_cells]
    exit_code = 1 if failed_arms else 0
    print(f"\nTotal: {total_satisfied}/{total_planned} satisfied")
    print(f"Exit: {exit_code}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Matrix queue: retry-aware benchmark runner for OpenBench.")
    parser.add_argument("--spec", required=True,
                        help="TOML spec file defining arms, task groups, retry budgets")
    args = parser.parse_args(argv)

    spec_path = os.path.abspath(args.spec)
    if not os.path.isfile(spec_path):
        print(f"ERROR: spec file not found: {spec_path}", file=sys.stderr)
        return 1

    spec_dir = os.path.dirname(spec_path)
    cwd = os.getcwd()

    try:
        spec = load_spec(spec_path)
    except SpecError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return run_matrix(spec, spec_dir, cwd)


if __name__ == "__main__":
    raise SystemExit(main())
