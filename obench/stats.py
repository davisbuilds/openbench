#!/usr/bin/env python3
"""Canonical statistics tool for OpenBench JSONL results.

Exclusion rules (single source of truth for headline stats):

* Rows whose ``failure_class`` is ``infra`` or ``rate_limited`` are excluded
  from solve-rate denominators entirely. They are counted in the exclusion
  report by failure class.
* Rows for tasks whose task directory contains ``DROPPED.md`` are quarantined:
  they are excluded from denominators and reported separately as dropped-task
  quarantines. Task directories are resolved from ``--tasks-dir`` roots; by
  default the tool checks ``tasks/`` and ``tasks-imported/terminal-bench/``.
* Every other structurally usable row counts in denominators, whether solved or
  failed. Nothing is silently dropped.

The CLI prints an all-countable table labelled non-comparable, plus (when two
or more groups are present) a matched-denominator table restricted to cells that
are present in every compared group. Wilson 95% confidence intervals use a pure
stdlib implementation.
"""

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict

from .paths import SOURCE_ROOT, default_imported_tasks_dir, default_tasks_dir, find_repo_root

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = SOURCE_ROOT


def _default_task_dirs():
    dirs = []
    tasks = default_tasks_dir()
    if tasks:
        dirs.append(tasks)
    root = find_repo_root()
    if root:
        tb = os.path.join(root, "tasks-imported", "terminal-bench")
        if os.path.isdir(tb):
            dirs.append(tb)
    elif default_imported_tasks_dir():
        pass
    return tuple(dirs) if dirs else (
        os.path.join(os.getcwd(), "tasks"),
        os.path.join(os.getcwd(), "tasks-imported", "terminal-bench"),
    )


DEFAULT_TASK_DIRS = _default_task_dirs()
# Defined below from failure_class -- the single source of truth. A locally
# hardcoded copy here silently missed "stalled" when it was added.
GROUP_CHOICES = ("harness,model", "model", "harness")
PROVENANCE_CORE_FIELDS = ("image_digest", "harness_version", "harness_version_source", "timeout_s")
PROVENANCE_CHECKER_FIELDS = (
    "checker_digest",
    "checker_sha256",
    "checker_hash",
    "checker_version",
    "checker_image_digest",
    "checker_config_digest",
    "checker_timeout_s",
)
Z_95 = 1.96

from .failure_class import class_for_report
from .failure_class import EXCLUDED_FROM_SOLVE_RATE as _EXCLUDED_FROM_SOLVE_RATE
from .harbor_job import (
    COMPARISON_PLAN_SCHEMA_VERSION,
    canonical_comparison_plan_bytes,
)
from . import usage_evidence

EXCLUDED_FAILURE_CLASSES = set(_EXCLUDED_FROM_SOLVE_RATE)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_nonnegative_number(value):
    return is_number(value) and value >= 0


def wilson_ci(successes, n, z=Z_95):
    """Return Wilson score 95% CI for ``successes / n``, clamped to [0, 1]."""
    if n <= 0:
        return (0.0, 1.0)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = phat + z2 / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n))
    return (max(0.0, (center - margin) / denom), min(1.0, (center + margin) / denom))


def median(values):
    vals = sorted(float(v) for v in values if is_number(v))
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def load_rows(paths):
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    rows.append({
                        "_invalid_json": str(exc),
                        "_source": path,
                        "_lineno": lineno,
                    })
                    continue
                if not isinstance(row, dict):
                    row = {"_invalid_json": "row is not an object"}
                row.setdefault("_source", path)
                row.setdefault("_lineno", lineno)
                rows.append(row)
    return rows


def parse_tasks_dirs(values):
    if not values:
        return list(DEFAULT_TASK_DIRS)
    roots = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                roots.append(os.path.abspath(part))
    return roots


def _safe_join_under(root, *parts):
    root_real = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root_real, *parts))
    if os.path.commonpath([root_real, candidate]) != root_real:
        return None
    return candidate


def _candidate_task_dirs(task, roots):
    task = str(task or "").strip()
    if not task:
        return []
    candidates = []
    if os.path.isabs(task):
        task_real = os.path.realpath(task)
        for root in roots:
            root_real = os.path.realpath(root)
            if os.path.commonpath([root_real, task_real]) == root_real:
                candidates.append(task_real)
    else:
        task_parts = task.split("/")
        if any(part in ("", ".", "..") for part in task_parts):
            return []
        split_task = task.split("/", 1)
        for root in roots:
            candidate = _safe_join_under(root, task)
            if candidate:
                candidates.append(candidate)
            if len(split_task) == 2 and os.path.basename(os.path.normpath(root)) == split_task[0]:
                candidate = _safe_join_under(root, split_task[1])
                if candidate:
                    candidates.append(candidate)
    # Preserve order while removing duplicates.
    seen = set()
    out = []
    for cand in candidates:
        norm = os.path.normpath(cand)
        if norm not in seen:
            out.append(norm)
            seen.add(norm)
    return out


def task_is_dropped(task, roots, cache):
    if task in cache:
        return cache[task]
    for task_dir in _candidate_task_dirs(task, roots):
        dropped = os.path.join(task_dir, "DROPPED.md")
        if os.path.isfile(dropped):
            cache[task] = dropped
            return dropped
    cache[task] = None
    return None


# Display bases for tok/slv and HTML badges. Cache-read is never folded into
# the comparable fresh total (see proxy_fresh_tokens / effective_tokens).
TOKEN_BASIS_SELF = "self-reported"
TOKEN_BASIS_PROXY = "proxy-measured"
TOKEN_BASIS_UNMETERED = "unmetered"


def proxy_fresh_tokens(row):
    """Proxy-metered fresh total: uncached input + output.

    Matches native adapters' ``tokens`` scalar (codex/pi/opencode/claude):
    fresh = tokens_input_uncached + tokens_output. Cache-read and cache-write
    stay in their own columns and are not mixed into this number. Reasoning is
    not added separately — ``tokens_proxy_output`` follows the provider's
    output field the same way native ``tokens_output`` does.
    """
    inp = row.get("tokens_proxy_input_uncached")
    out = row.get("tokens_proxy_output")
    if is_nonnegative_number(inp) and is_nonnegative_number(out):
        return float(inp) + float(out)
    return None


def effective_tokens(row):
    """Return ``(fresh_tokens, basis)`` for comparable tok/slv aggregation.

    Priority:
      1. Self-reported ``tokens`` when present → ``self-reported``.
      2. Else, when ``token_basis_proxy == "proxy_measured"``, the proxy fresh
         total (uncached input + output) → ``proxy-measured``.
      3. Else, when ``token_basis == "unmetered"`` → ``(None, "unmetered")``.
      4. Else → ``(None, None)``.

    Older rows without proxy fields behave as before: only a present ``tokens``
    scalar contributes. Large ``tokens_proxy_cache_read`` never inflates the
    fresh total.
    """
    if not usage_evidence.ranking_eligible(row):
        return None, None
    if is_nonnegative_number(row.get("tokens")):
        return float(row["tokens"]), TOKEN_BASIS_SELF
    if row.get("token_basis_proxy") == "proxy_measured":
        proxy = proxy_fresh_tokens(row)
        if proxy is not None:
            return proxy, TOKEN_BASIS_PROXY
    if row.get("token_basis") == "unmetered":
        return None, TOKEN_BASIS_UNMETERED
    return None, None


def display_token_basis(row):
    """Normalize a row's token accounting into a badge label, or None."""
    evidence_label = usage_evidence.display_label(row)
    if evidence_label is not None:
        return evidence_label
    _value, basis = effective_tokens(row)
    if basis is not None:
        return basis
    raw = row.get("token_basis") or row.get("token_basis_proxy")
    if raw in (None, ""):
        return None
    text = str(raw)
    if text == "proxy_measured":
        return TOKEN_BASIS_PROXY
    if text == "unmetered":
        return TOKEN_BASIS_UNMETERED
    # vendor_split / harness_reported / estimated / scalar_exact / ...
    return TOKEN_BASIS_SELF


def total_tokens(row):
    if not usage_evidence.ranking_eligible(row):
        return None
    if is_nonnegative_number(row.get("tokens_total")):
        return row.get("tokens_total")
    value, _basis = effective_tokens(row)
    return value


def input_tokens(row):
    if not usage_evidence.ranking_eligible(row):
        return None
    if is_nonnegative_number(row.get("tokens_input")):
        return row.get("tokens_input")
    if is_nonnegative_number(row.get("tokens_input_uncached")):
        return row.get("tokens_input_uncached")
    if row.get("token_basis_proxy") == "proxy_measured":
        proxy = row.get("tokens_proxy_input_uncached")
        if is_nonnegative_number(proxy):
            return proxy
    return None


def output_tokens(row):
    if not usage_evidence.ranking_eligible(row):
        return None
    if is_nonnegative_number(row.get("tokens_output")):
        return row.get("tokens_output")
    if row.get("token_basis_proxy") == "proxy_measured":
        proxy = row.get("tokens_proxy_output")
        if is_nonnegative_number(proxy):
            return proxy
    return None


def load_pricing(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("pricing JSON must be an object keyed by model")
    pricing = {}
    for model, item in data.items():
        if not isinstance(item, dict):
            continue
        inp = item.get("input_per_mtok")
        out = item.get("output_per_mtok")
        if is_nonnegative_number(inp) and is_nonnegative_number(out):
            pricing[str(model)] = {
                "input_per_mtok": float(inp),
                "output_per_mtok": float(out),
            }
    return pricing


def row_cost(row, pricing):
    if not pricing:
        return None
    model = str(row.get("model") or "")
    price = pricing.get(model)
    if not price:
        return None
    inp = input_tokens(row)
    out = output_tokens(row)
    if not is_number(inp) or not is_number(out):
        return None
    return (float(inp) / 1_000_000.0 * price["input_per_mtok"]
            + float(out) / 1_000_000.0 * price["output_per_mtok"])


def group_fields(group_arg):
    return tuple(part.strip() for part in group_arg.split(",") if part.strip())


def is_valid_result_row(row):
    """Return True for rows with the minimum schema needed for canonical stats."""
    if not isinstance(row, dict) or "_invalid_json" in row:
        return False
    if not all(isinstance(row.get(field), str) and row.get(field)
               for field in ("harness", "model", "task")):
        return False
    if not isinstance(row.get("trial"), int) or isinstance(row.get("trial"), bool):
        return False
    if not isinstance(row.get("success"), bool):
        return False
    return True


def group_key(row, fields):
    key = tuple(str(row.get(field) or "-") for field in fields)
    provenance = row.get("candidate_provenance")
    if (
        isinstance(provenance, dict)
        and provenance.get("kind") == "harbor_job"
        and provenance.get("comparison_arm_id")
    ):
        key += (str(provenance["comparison_arm_id"]),)
    return key


def group_label(key, fields):
    suffix = f",arm={key[-1]}" if len(key) > len(fields) else ""
    if len(fields) == 1:
        return key[0] + suffix
    return ",".join(
        f"{field}={value}" for field, value in zip(fields, key)
    ) + suffix


def is_harbor_result_row(row):
    provenance = row.get("candidate_provenance")
    return (
        row.get("exec_mode") == "harbor"
        or (
            isinstance(provenance, dict)
            and provenance.get("kind") == "harbor_job"
        )
    )


def _comparison_plan_arm(row):
    provenance = row.get("candidate_provenance")
    if not isinstance(provenance, dict):
        raise ValueError(
            "Harbor matched comparison requires exact OpenBench "
            "comparison-plan identity"
        )
    plan = provenance.get("comparison_plan")
    expected_plan_fields = {
        "schema_version",
        "harbor_version",
        "harbor_git_commit_hash",
        "job_name",
        "job_config_sha256",
        "attempts",
        "dataset",
        "tasks",
        "arms",
    }
    if not isinstance(plan, dict) or set(plan) != expected_plan_fields:
        raise ValueError(
            "Harbor matched comparison requires a canonical embedded plan"
        )
    try:
        plan_bytes = canonical_comparison_plan_bytes(plan)
    except (TypeError, ValueError):
        raise ValueError(
            "Harbor matched comparison requires a canonical embedded plan"
        ) from None
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    if (
        plan.get("schema_version") != COMPARISON_PLAN_SCHEMA_VERSION
        or provenance.get("comparison_plan_schema_version")
        != COMPARISON_PLAN_SCHEMA_VERSION
        or provenance.get("comparison_plan_sha256") != plan_sha256
    ):
        raise ValueError(
            "Harbor matched comparison embedded plan digest does not match"
        )
    arms = plan.get("arms")
    if not isinstance(arms, list) or not arms:
        raise ValueError("Harbor matched comparison plan has invalid arms")
    expected_arm_fields = {
        "arm_id",
        "agent_config_name",
        "harbor_model_name",
        "agent_config_sha256",
        "canonical_harness",
        "canonical_model",
    }
    arms_by_id = {}
    digests = set()
    for arm in arms:
        if not isinstance(arm, dict) or set(arm) != expected_arm_fields:
            raise ValueError("Harbor matched comparison plan has invalid arms")
        arm_id = arm.get("arm_id")
        digest = arm.get("agent_config_sha256")
        if (
            not isinstance(arm_id, str)
            or not arm_id
            or arm_id in arms_by_id
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or digest in digests
        ):
            raise ValueError("Harbor matched comparison plan has invalid arms")
        arms_by_id[arm_id] = arm
        digests.add(digest)
    arm = arms_by_id.get(provenance.get("comparison_arm_id"))
    if (
        arm is None
        or arm["agent_config_sha256"]
        != provenance.get("agent_config_sha256")
        or arm["canonical_harness"] != row.get("harness")
        or arm["canonical_model"] != row.get("model")
    ):
        raise ValueError(
            "Harbor matched comparison row does not match its declared plan arm"
        )
    return arm


def comparison_cell_key(row, *, require_harbor_identity=True):
    """Return the supported matched-cell identity for one normalized row."""

    if not is_harbor_result_row(row):
        return (row.get("task"), row.get("trial"))
    provenance = row.get("candidate_provenance")
    if not isinstance(provenance, dict):
        if require_harbor_identity:
            raise ValueError(
                "Harbor matched comparison requires exact OpenBench "
                "comparison-plan identity"
            )
        return (row.get("task"), row.get("trial"))
    schema = provenance.get("comparison_plan_schema_version")
    plan_sha256 = provenance.get("comparison_plan_sha256")
    arm_id = provenance.get("comparison_arm_id")
    agent_config_sha256 = provenance.get("agent_config_sha256")
    resolved_tasks = provenance.get("comparison_resolved_tasks")
    block = provenance.get("comparison_block")
    exact = (
        schema == COMPARISON_PLAN_SCHEMA_VERSION
        and isinstance(plan_sha256, str)
        and len(plan_sha256) == 64
        and all(char in "0123456789abcdef" for char in plan_sha256)
        and isinstance(arm_id, str)
        and bool(arm_id)
        and isinstance(agent_config_sha256, str)
        and len(agent_config_sha256) == 64
        and all(char in "0123456789abcdef" for char in agent_config_sha256)
        and isinstance(resolved_tasks, list)
        and bool(resolved_tasks)
        and resolved_tasks == sorted(set(resolved_tasks))
        and isinstance(block, dict)
        and set(block) == {"task", "index"}
        and block.get("task") == row.get("task")
        and block.get("task") in resolved_tasks
        and block.get("index") == row.get("trial")
        and isinstance(block.get("index"), int)
        and not isinstance(block.get("index"), bool)
        and block["index"] >= 1
        and provenance.get("trial_mapping")
        == "openbench_comparison_plan_v2"
        and provenance.get("temporal_matched_block_claim") is False
    )
    if not exact:
        if require_harbor_identity:
            raise ValueError(
                "Harbor matched comparison requires exact OpenBench "
                "comparison-plan identity"
            )
        return (row.get("task"), row.get("trial"))
    _comparison_plan_arm(row)
    return (plan_sha256, block["task"], block["index"])


def validate_matched_comparison_rows(rows):
    """Return the matched identity mode or reject unsupported mixed evidence."""

    rows = list(rows)
    harbor_rows = [row for row in rows if is_harbor_result_row(row)]
    if not harbor_rows:
        return "legacy_task_trial"
    if len(harbor_rows) != len(rows):
        raise ValueError(
            "matched comparison cannot mix Harbor comparison-plan identity "
            "with legacy non-Harbor task/trial identity"
        )
    keys = [comparison_cell_key(row) for row in harbor_rows]
    plan_sha256s = {key[0] for key in keys}
    if len(plan_sha256s) != 1:
        raise ValueError(
            "Harbor matched comparison requires one exact comparison-plan "
            "digest across every arm"
        )
    resolved_task_sets = {
        tuple(row["candidate_provenance"]["comparison_resolved_tasks"])
        for row in harbor_rows
    }
    if len(resolved_task_sets) != 1:
        raise ValueError(
            "Harbor matched comparison requires one lock-resolved task set "
            "across every arm"
        )
    arm_bindings = defaultdict(set)
    for row in harbor_rows:
        _comparison_plan_arm(row)
        provenance = row["candidate_provenance"]
        arm_bindings[provenance["comparison_arm_id"]].add((
            provenance["agent_config_sha256"],
            row.get("harness"),
            row.get("model"),
        ))
    if any(len(bindings) != 1 for bindings in arm_bindings.values()):
        raise ValueError(
            "Harbor matched comparison arm changed rendered config or "
            "canonical labels"
        )
    return "harbor_comparison_plan"


def matched_cell_key(row, fields):
    parts = [str(value) for value in comparison_cell_key(row)]
    for field in ("harness", "model"):
        if field not in fields:
            parts.append(str(row.get(field) or "-"))
    return tuple(parts)


def _present_provenance_value(value):
    if value is None or value == "":
        return False
    try:
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _jsonish(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _docker_entry_timeout(row):
    cmd = row.get("cmd")
    if not isinstance(cmd, dict):
        return None
    docker = cmd.get("docker")
    if not isinstance(docker, list):
        return None
    try:
        entry_idx = docker.index("/bench/entry.py")
    except ValueError:
        return None
    timeout_idx = entry_idx + 3
    if timeout_idx < len(docker):
        candidate = docker[timeout_idx]
        if _present_provenance_value(candidate):
            return candidate
    return None


def provenance_value(row, field):
    if field == "timeout_s":
        for key in ("timeout_s", "timeout_config", "adapter_timeout_s", "harness_timeout_s", "timeout"):
            value = row.get(key)
            if _present_provenance_value(value):
                return value
        return _docker_entry_timeout(row)
    value = row.get(field)
    return value if _present_provenance_value(value) else None


def provenance_fields_for_rows(rows):
    fields = list(PROVENANCE_CORE_FIELDS)
    for field in PROVENANCE_CHECKER_FIELDS:
        if any(provenance_value(row, field) is not None for row in rows):
            fields.append(field)
    return fields


def _format_group_sets(group_sets):
    parts = []
    for group in sorted(group_sets):
        values = ", ".join(repr(value) for value in group_sets[group])
        parts.append(f"{group}: [{values}]")
    return "{" + ", ".join(parts) + "}"


def build_provenance(countable_rows, fields):
    by_group = defaultdict(list)
    for row in countable_rows:
        by_group[group_label(group_key(row, fields), fields)].append(row)

    unknown_rows = sum(1 for row in countable_rows if provenance_value(row, "image_digest") is None)
    out = {
        "checked": len(by_group) >= 2,
        "ok": True,
        "unknown_provenance_rows": unknown_rows,
        "info": [],
        "fields": {},
        "flags": [],
        "shared": {},
    }
    if unknown_rows:
        out["info"].append(f"unknown provenance: {unknown_rows} rows")
    if len(by_group) < 2:
        return out

    for field in provenance_fields_for_rows(countable_rows):
        # Harness-scoped fields (each harness has its own CLI version) are
        # compared per harness: pi 0.80.6 alongside codex 0.144.0 in one group
        # is normal; the SAME harness at two versions is the confound.
        harness_scoped = field in ("harness_version", "harness_version_source")
        per_group = {}
        missing_counts = {}
        for group in sorted(by_group):
            values = []
            missing = 0
            for row in by_group[group]:
                value = provenance_value(row, field)
                if value is None:
                    missing += 1
                    continue
                if harness_scoped:
                    values.append(f"{row.get('harness')}={_jsonish(value)}")
                else:
                    values.append(_jsonish(value))
            per_group[group] = sorted(set(values))
            missing_counts[group] = missing

        out["fields"][field] = {
            "values_by_group": per_group,
            "missing_by_group": missing_counts,
        }
        groups_with_values = {group: values for group, values in per_group.items() if values}
        missing_total = sum(missing_counts.values())
        if field != "image_digest" and groups_with_values and missing_total:
            out["info"].append(f"missing {field}: {missing_total} rows")
        field_flagged = False

        for group, values in groups_with_values.items():
            if harness_scoped:
                by_harness = defaultdict(set)
                for value in values:
                    harness, _, version = value.partition("=")
                    by_harness[harness].add(version)
                mixed = {h: sorted(v) for h, v in by_harness.items() if len(v) > 1}
                if mixed:
                    out["flags"].append({
                        "type": "mixed_within_group",
                        "field": field,
                        "group": group,
                        "values": mixed,
                        "message": f"{field} has mixed values within group {group}: {mixed}",
                    })
                    field_flagged = True
            elif len(values) > 1:
                out["flags"].append({
                    "type": "mixed_within_group",
                    "field": field,
                    "group": group,
                    "values": values,
                    "message": f"{field} has mixed values within group {group}: {values}",
                })
                field_flagged = True

        if len(groups_with_values) >= 2:
            if harness_scoped:
                # Compare only harnesses present in every group; lane coverage
                # gaps are a denominator issue (matched table), not provenance.
                versions_by_group = {}
                for group, values in groups_with_values.items():
                    by_harness = defaultdict(set)
                    for value in values:
                        harness, _, version = value.partition("=")
                        by_harness[harness].add(version)
                    versions_by_group[group] = by_harness
                common = set.intersection(*(set(v) for v in versions_by_group.values()))
                diffs = {}
                for harness in sorted(common):
                    sets = {group: sorted(versions_by_group[group][harness]) for group in versions_by_group}
                    if len({tuple(v) for v in sets.values()}) > 1:
                        diffs[harness] = sets
                if diffs:
                    out["flags"].append({
                        "type": "differs_across_groups",
                        "field": field,
                        "values_by_group": diffs,
                        "message": f"{field} differs across groups for the same harness: {diffs}",
                    })
                    field_flagged = True
            else:
                distinct_sets = {tuple(values) for values in groups_with_values.values()}
                if len(distinct_sets) > 1:
                    out["flags"].append({
                        "type": "differs_across_groups",
                        "field": field,
                        "values_by_group": groups_with_values,
                        "message": f"{field} differs across groups: {_format_group_sets(groups_with_values)}",
                    })
                    field_flagged = True

        if not field_flagged and groups_with_values:
            first_values = next(iter(groups_with_values.values()))
            if all(values == first_values for values in groups_with_values.values()):
                out["shared"][field] = first_values

    out["ok"] = not out["flags"]
    return out


def render_provenance_banner(provenance):
    info = provenance.get("info") or []
    info_text = ("; " + "; ".join(info)) if info else ""
    if provenance.get("ok"):
        if not provenance.get("checked"):
            return "PROVENANCE: OK (fewer than 2 groups; comparison provenance not checked" + info_text + ")"
        shared = provenance.get("shared") or {}
        if shared:
            parts = []
            for field in sorted(shared):
                values = shared[field]
                rendered = values[0] if len(values) == 1 else "[" + ", ".join(values) + "]"
                parts.append(f"{field}={rendered}")
            detail = "all compared groups share " + ", ".join(parts)
        else:
            detail = "no comparable provenance fields recorded"
        return f"PROVENANCE: OK ({detail}{info_text})"

    messages = [flag["message"] for flag in provenance.get("flags", [])]
    lines = ["!" * 72, "NON-COMPARABLE: " + "; ".join(messages)]
    if info:
        lines.append("PROVENANCE INFO: " + "; ".join(info))
    lines.append("!" * 72)
    return "\n".join(lines)


def _empty_acc(label):
    return {
        "group": label,
        "solved": 0,
        "n": 0,
        "score_values": [],
        "solved_wall_time_s": [],
        "solved_t_agent_s": [],
        "solved_tokens_total": [],
        "solved_tokens_input": [],
        "solved_tokens_output": [],
        "solved_cost": [],
    }


def _add_row(acc, row, pricing):
    success = bool(row.get("success"))
    acc["n"] += 1
    if success:
        acc["solved"] += 1
        wall = row.get("wall_time_s")
        if is_nonnegative_number(wall):
            # Proxy-injected pacing waits are measurement plumbing, not model
            # latency; report.py already subtracts them and every reader must
            # agree (paced arms would otherwise look slower than unpaced ones).
            paced = row.get("paced_wait_s")
            if is_nonnegative_number(paced):
                wall = max(0.0, wall - paced)
            acc["solved_wall_time_s"].append(wall)
        agent_time = row.get("t_agent_s")
        if is_nonnegative_number(agent_time):
            acc["solved_t_agent_s"].append(agent_time)
        tok_total = total_tokens(row)
        if is_number(tok_total):
            acc["solved_tokens_total"].append(tok_total)
        tok_input = input_tokens(row)
        if is_number(tok_input):
            acc["solved_tokens_input"].append(tok_input)
        tok_output = output_tokens(row)
        if is_number(tok_output):
            acc["solved_tokens_output"].append(tok_output)
        cost = row_cost(row, pricing)
        if is_number(cost):
            acc["solved_cost"].append(cost)
    score = row.get("score")
    if not is_number(score) or not (0.0 <= float(score) <= 1.0):
        score = 1.0 if success else 0.0
    acc["score_values"].append(float(score))


def summarize_acc(acc, min_n, include_cost):
    n = acc["n"]
    solved = acc["solved"]
    lo, hi = wilson_ci(solved, n)
    rate = solved / n if n else None
    score = (sum(acc["score_values"]) / len(acc["score_values"])) if acc["score_values"] else None
    out = {
        "group": acc["group"],
        "solved": solved,
        "n": n,
        "solve_rate": rate,
        "wilson95": [lo, hi],
        "mean_score": score,
        "median_wall_time_s_solved": median(acc["solved_wall_time_s"]),
        "median_t_agent_s_solved": median(acc["solved_t_agent_s"]),
        "median_tokens_total_solved": median(acc["solved_tokens_total"]),
        "median_tokens_input_solved": median(acc["solved_tokens_input"]),
        "median_tokens_output_solved": median(acc["solved_tokens_output"]),
        "low_n": n < min_n,
        "flags": ["LOW-N"] if n < min_n else [],
    }
    if include_cost:
        out["median_cost_solved"] = median(acc["solved_cost"])
    return out


def aggregate_table(rows, fields, min_n, pricing=None):
    accs = {}
    for row in rows:
        key = group_key(row, fields)
        label = group_label(key, fields)
        acc = accs.setdefault(key, _empty_acc(label))
        _add_row(acc, row, pricing)
    include_cost = pricing is not None
    return [summarize_acc(accs[key], min_n, include_cost) for key in sorted(accs)]


def filter_rows(rows, tasks_dirs):
    countable = []
    excluded_counts = Counter()
    quarantined_tasks = Counter()
    invalid_rows = 0
    dropped_cache = {}

    for row in rows:
        if "_invalid_json" in row:
            invalid_rows += 1
            excluded_counts["invalid_json"] += 1
            continue
        if not is_valid_result_row(row):
            invalid_rows += 1
            excluded_counts["invalid_row"] += 1
            continue
        task = row.get("task")
        dropped_path = task_is_dropped(task, tasks_dirs, dropped_cache)
        if dropped_path:
            excluded_counts["quarantined_dropped_task"] += 1
            quarantined_tasks[str(task)] += 1
            continue
        fc = class_for_report(row)
        if fc in EXCLUDED_FAILURE_CLASSES:
            excluded_counts[fc] += 1
            continue
        countable.append(row)
    return {
        "countable_rows": countable,
        "excluded_counts": dict(sorted(excluded_counts.items())),
        "quarantined_tasks": dict(sorted(quarantined_tasks.items())),
        "invalid_rows": invalid_rows,
    }


def matched_rows(countable_rows, fields):
    rows_by_group = defaultdict(list)
    for row in countable_rows:
        gkey = group_key(row, fields)
        rows_by_group[gkey].append(row)
    if len(rows_by_group) < 2:
        return list(countable_rows), None

    comparison_identity = validate_matched_comparison_rows(countable_rows)
    by_group = defaultdict(lambda: defaultdict(list))
    for gkey, rows in rows_by_group.items():
        for row in rows:
            ckey = matched_cell_key(row, fields)
            by_group[gkey][ckey].append(row)

    duplicate_cells = 0
    duplicate_rows = 0
    unique_by_group = {}
    for gkey, cells in by_group.items():
        unique_by_group[gkey] = {}
        for ckey, rows in cells.items():
            if len(rows) == 1:
                unique_by_group[gkey][ckey] = rows[0]
            else:
                # Duplicate benchmark cells are ambiguous for a matched table.
                # Keep all rows in all-countable stats, but exclude the duplicated
                # cell from matched denominators and report it explicitly.
                duplicate_cells += 1
                duplicate_rows += len(rows)

    common = None
    for cells in unique_by_group.values():
        keys = set(cells)
        common = keys if common is None else common & keys
    common = common or set()
    out = []
    for gkey in sorted(unique_by_group):
        for ckey in sorted(common):
            out.append(unique_by_group[gkey][ckey])
    diagnostics = {
        "groups_compared": len(by_group),
        "matched_cells_per_group": len(common),
        "matched_rows": len(out),
        "unmatched_countable_rows": len(countable_rows) - len(out),
        "duplicate_cells_excluded": duplicate_cells,
        "duplicate_rows_excluded": duplicate_rows,
        "comparison_identity": comparison_identity,
    }
    return out, diagnostics


def build_stats(paths, group="harness,model", min_n=5, tasks_dirs=None, pricing=None):
    fields = group_fields(group)
    if group not in GROUP_CHOICES:
        raise ValueError(f"unsupported group {group!r}")
    tasks_dirs = parse_tasks_dirs(tasks_dirs)
    rows = load_rows(paths)
    filtered = filter_rows(rows, tasks_dirs)
    countable = filtered["countable_rows"]
    overall = aggregate_table(countable, fields, min_n, pricing=pricing)
    mrows, mdiag = matched_rows(countable, fields)
    matched = aggregate_table(mrows, fields, min_n, pricing=pricing) if mdiag else None
    provenance = build_provenance(countable, fields)
    return {
        "inputs": list(paths),
        "group": group,
        "group_fields": list(fields),
        "min_n": min_n,
        "tasks_dirs": tasks_dirs,
        "raw_rows": len(rows),
        "countable_rows": len(countable),
        "excluded_counts": filtered["excluded_counts"],
        "quarantined_tasks": filtered["quarantined_tasks"],
        "tables": {
            "all_countable_non_comparable": overall,
            "matched_comparable": matched,
        },
        "matched": mdiag,
        "provenance_ok": provenance["ok"],
        "provenance": provenance,
        "pricing": {"enabled": pricing is not None},
    }


def fmt_pct(value):
    return "-" if value is None else f"{value * 100:.1f}%"


def fmt_num(value, digits=1):
    return "-" if value is None else f"{float(value):.{digits}f}"


def fmt_tokens(value):
    if value is None:
        return "-"
    value = float(value)
    return f"{value / 1000:.1f}k" if abs(value) >= 1000 else f"{value:.0f}"


def fmt_cost(value):
    return "-" if value is None else f"${float(value):.4f}"


def render_table(rows, include_cost=False):
    headers = [
        "group", "solved", "n", "rate", "wilson95", "score",
        "med_s/solve", "med_agent_s/solve", "med_tok/solve", "med_in/solve", "med_out/solve",
        "flags",
    ]
    if include_cost:
        headers.insert(-1, "med_cost/solve")
    body = []
    for row in rows:
        cells = [
            row["group"],
            str(row["solved"]),
            str(row["n"]),
            fmt_pct(row["solve_rate"]),
            f"[{row['wilson95'][0]:.3f}, {row['wilson95'][1]:.3f}]",
            fmt_num(row["mean_score"], 3),
            fmt_num(row["median_wall_time_s_solved"], 2),
            fmt_num(row["median_t_agent_s_solved"], 2),
            fmt_tokens(row["median_tokens_total_solved"]),
            fmt_tokens(row["median_tokens_input_solved"]),
            fmt_tokens(row["median_tokens_output_solved"]),
            ",".join(row["flags"]) if row["flags"] else "-",
        ]
        if include_cost:
            cells.insert(-1, fmt_cost(row.get("median_cost_solved")))
        body.append(cells)
    widths = [len(h) for h in headers]
    for cells in body:
        for idx, cell in enumerate(cells):
            widths[idx] = max(widths[idx], len(cell))

    def line(cells):
        return "  ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(cells))

    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(cells) for cells in body])


def render_text(stats):
    lines = []
    lines.append("OpenBench canonical stats")
    lines.append(f"Inputs: {', '.join(stats['inputs'])}")
    lines.append(f"Group: {stats['group']}   min_n: {stats['min_n']}")
    lines.append(f"Rows: raw={stats['raw_rows']} countable={stats['countable_rows']}")
    excluded = stats["excluded_counts"] or {}
    if excluded:
        lines.append("Excluded: " + ", ".join(f"{k}={v}" for k, v in sorted(excluded.items())))
    else:
        lines.append("Excluded: none")
    if stats["quarantined_tasks"]:
        lines.append("Quarantined dropped tasks: " + ", ".join(
            f"{task}={count}" for task, count in sorted(stats["quarantined_tasks"].items())))
    lines.append("")
    provenance = stats.get("provenance") or {"ok": True, "checked": False}
    provenance_banner = render_provenance_banner(provenance)
    lines.append(provenance_banner)
    lines.append("")
    lines.append("ALL COUNTABLE ROWS (NON-COMPARABLE; denominators may differ)")
    include_cost = stats.get("pricing", {}).get("enabled", False)
    lines.append(render_table(stats["tables"]["all_countable_non_comparable"], include_cost=include_cost))
    matched = stats.get("matched")
    if matched:
        lines.append("")
        if not provenance.get("ok", True):
            lines.append(provenance_banner)
            lines.append("")
        lines.append("MATCHED DENOMINATORS (COMPARABLE; cells present in every group)")
        diag = (
            f"Matched cells/group={matched['matched_cells_per_group']} "
            f"groups={matched['groups_compared']} unmatched_countable_rows={matched['unmatched_countable_rows']}"
        )
        if matched.get("duplicate_cells_excluded"):
            diag += (
                f" duplicate_cells_excluded={matched['duplicate_cells_excluded']}"
                f" duplicate_rows_excluded={matched['duplicate_rows_excluded']}"
            )
        lines.append(diag)
        lines.append(render_table(stats["tables"]["matched_comparable"], include_cost=include_cost))
    return "\n".join(lines)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Canonical OpenBench stats from results JSONL")
    parser.add_argument("results", nargs="+", help="results JSONL file(s)")
    parser.add_argument("--group", choices=GROUP_CHOICES, default="harness,model",
                        help="comparison grouping (default: harness,model)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--min-n", type=int, default=5, help="mark groups with n below this as LOW-N")
    parser.add_argument("--tasks-dir", action="append",
                        help="task root to inspect for DROPPED.md; may be repeated or comma-separated")
    parser.add_argument("--pricing", help="optional pricing JSON: {model: {input_per_mtok, output_per_mtok}}")
    parser.add_argument("--strict-provenance", action="store_true",
                        help="exit 2 when grouped comparison provenance differs")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    pricing = load_pricing(args.pricing) if args.pricing else None
    stats = build_stats(
        args.results,
        group=args.group,
        min_n=args.min_n,
        tasks_dirs=args.tasks_dir,
        pricing=pricing,
    )
    if args.json:
        print(json.dumps(stats, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(render_text(stats))
    if args.strict_provenance and not stats["provenance_ok"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
