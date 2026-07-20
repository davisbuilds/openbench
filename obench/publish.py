#!/usr/bin/env python3
"""Publish and verify shareable OpenBench comparison bundles.

``obench publish`` turns gate-passing candidate runs into a self-contained
artifact (HTML card + filtered results + provenance + README) that a third
party can post. Transcripts are never bundled. ``obench verify`` recomputes
digests recorded in provenance.json so shared claims are tamper-evident
without a server.

What verify proves: the bundled results.jsonl still matches its recorded
SHA-256, and per-task content digests still match when the referenced task
trees are available. What it does NOT prove: runs were not cherry-picked or
rerun until green — publish the full matrix.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone

from . import __version__
from . import compare
from . import report_page
from . import scrub
from . import stats
from .paths import default_results_path, default_tasks_dir, resolve_tasks_dir

TRANSCRIPT_FIELD_KEYS = (
    "transcript_path",
    "transcript",
    "transcripts_dir",
    "full_output",
    "output_tail",
    "checker_stdout",
    "checker_stderr",
)
# Injected by stats.load_rows; absolute paths here trip the publish PII check.
LOAD_META_FIELD_KEYS = ("_source", "_lineno")
# High-signal PII classes for publish bundles. Hex/base64 catch-alls are omitted
# because provenance digests and image digests are intentional 32+ hex strings.
PUBLISH_PII_CATEGORIES = frozenset({
    "email", "api-key", "github-token", "slack-token", "aws-key",
    "home-path", "username", "hostname",
})
PUBLISH_METHODOLOGY = """## What this card is

A self-contained OpenBench comparison bundle: candidate harness arm(s)
highlighted against stock arms on the same result rows.

## What `obench verify` proves

- The bundled `results.jsonl` still matches the SHA-256 recorded in
  `provenance.json` (tamper-evident).
- When local task trees are available, per-task content digests still
  match under the bundle's digest scheme (scheme 2: instruction.md +
  checker.sh + workspace|workspace.toml + checker_data/; scheme 1 /
  legacy: same without checker_data/). Missing digests FAIL verification.

## What verify does NOT prove

- Runs were not cherry-picked or rerun until green.
- Auth material, secrets, or LOCAL-ONLY transcripts were reviewed
  (transcripts are never included in the bundle).
- Candidate admission-gate live checks were executed on this machine.

Publish the full matrix whenever possible.
"""


class PublishError(ValueError):
    """User-facing publish failure."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# Digest schemes for task_content_digest / provenance.json:
#   1 (legacy) — instruction.md + checker.sh + workspace.toml + workspace/
#                (no checker_data/). Bundles without digest_scheme use this.
#   2 (current) — scheme 1 plus checker_data/ (oracle inputs).
DIGEST_SCHEME_LEGACY = 1
DIGEST_SCHEME_CURRENT = 2


def _feed_tree_into_digest(hasher, task_dir: str, tree_name: str, feed) -> None:
    """Hash every regular file under ``task_dir/tree_name`` in stable order."""
    root_dir = os.path.join(task_dir, tree_name)
    if not os.path.isdir(root_dir):
        return
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            rel = os.path.relpath(full, task_dir).replace(os.sep, "/")
            with open(full, "rb") as fh:
                feed(rel, fh.read())


def resolve_digest_scheme(provenance) -> int:
    """Return the digest scheme for a provenance dict (missing → legacy 1)."""
    raw = provenance.get("digest_scheme") if isinstance(provenance, dict) else None
    if raw is None:
        return DIGEST_SCHEME_LEGACY
    try:
        scheme = int(raw)
    except (TypeError, ValueError) as exc:
        raise PublishError(f"invalid digest_scheme in provenance: {raw!r}") from exc
    if scheme not in (DIGEST_SCHEME_LEGACY, DIGEST_SCHEME_CURRENT):
        raise PublishError(f"unsupported digest_scheme: {scheme}")
    return scheme


def task_content_digest(task_dir: str, scheme: int = DIGEST_SCHEME_CURRENT) -> str:
    """SHA-256 over task oracle inputs under the given digest scheme.

    Scheme 1 (legacy): instruction.md + checker.sh + workspace.toml + workspace/.
    Scheme 2 (current): scheme 1 plus ``checker_data/`` so post-publish changes
    to checker-owned fixtures fail verify.

    Files are hashed in a stable path-sorted order. Missing optional pieces are
    skipped; at least instruction.md or checker.sh must exist.
    """
    if scheme not in (DIGEST_SCHEME_LEGACY, DIGEST_SCHEME_CURRENT):
        raise PublishError(f"unsupported digest_scheme: {scheme}")
    task_dir = os.path.abspath(task_dir)
    hasher = hashlib.sha256()
    found = False

    def _feed(rel: str, data: bytes):
        nonlocal found
        found = True
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(data)
        hasher.update(b"\0")

    for name in ("instruction.md", "checker.sh", "workspace.toml"):
        path = os.path.join(task_dir, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                _feed(name, fh.read())

    _feed_tree_into_digest(hasher, task_dir, "workspace", _feed)
    if scheme >= DIGEST_SCHEME_CURRENT:
        _feed_tree_into_digest(hasher, task_dir, "checker_data", _feed)

    if not found:
        raise PublishError(f"task dir has no hashable content: {task_dir}")
    return hasher.hexdigest()


def resolve_task_dir(task: str, tasks_dirs):
    """Return the first existing task directory under ``tasks_dirs``."""
    roots = stats.parse_tasks_dirs(tasks_dirs)
    for root in roots:
        path = os.path.join(root, task)
        if os.path.isdir(path):
            return path
    return None


def _arm_name(row):
    return compare._row_arm(row)


def _is_candidate_row(row):
    return isinstance(row.get("candidate_provenance"), dict)


def _candidate_name(row):
    prov = row.get("candidate_provenance")
    if isinstance(prov, dict) and prov.get("name"):
        return str(prov["name"])
    return None


def resolve_highlight_names(candidate_specs):
    """Map ``--candidate`` path-or-name args to highlight arm names."""
    names = []
    for spec in candidate_specs or []:
        if os.path.isfile(spec):
            try:
                from .candidates import load_candidate
                from .paths import default_adapters_dir
                names.append(load_candidate(spec, default_adapters_dir()).name)
            except Exception as exc:  # noqa: BLE001 - surface as publish error
                raise PublishError(f"could not load candidate {spec!r}: {exc}") from exc
        else:
            names.append(str(spec))
    return names


def strip_transcript_fields(row):
    """Return a shallow copy without LOCAL-ONLY transcript-bearing fields."""
    drop = set(TRANSCRIPT_FIELD_KEYS) | set(LOAD_META_FIELD_KEYS)
    cleaned = {key: value for key, value in row.items() if key not in drop}
    return cleaned


def _redact_local_path(value):
    if not isinstance(value, str) or not value:
        return value
    if value in (".",):
        return value
    if value.startswith(("http://", "https://", "git@", "ssh://")):
        return value
    if value.startswith("~/"):
        return value
    if os.path.isabs(value) or (os.sep in value) or ("/" in value):
        base = os.path.basename(value.rstrip("/\\"))
        return base or "<path>"
    return value


def sanitize_row_for_publish(row):
    """Strip transcripts and redact absolute local paths from provenance fields."""
    cleaned = strip_transcript_fields(row)
    prov = cleaned.get("candidate_provenance")
    if isinstance(prov, dict):
        safe = dict(prov)
        for key in ("spec", "config_dir"):
            if key in safe:
                safe[key] = _redact_local_path(safe[key])
        auth_files = safe.get("auth_files")
        if isinstance(auth_files, list):
            safe_auth = []
            for item in auth_files:
                if not isinstance(item, dict):
                    continue
                entry = dict(item)
                if "source" in entry:
                    entry["source"] = _redact_local_path(entry["source"])
                safe_auth.append(entry)
            safe["auth_files"] = safe_auth
        cleaned["candidate_provenance"] = safe
    ws = cleaned.get("workspace_source")
    if isinstance(ws, dict):
        safe_ws = dict(ws)
        if "repo" in safe_ws:
            safe_ws["repo"] = _redact_local_path(safe_ws["repo"])
        cleaned["workspace_source"] = safe_ws
    return cleaned


def rows_reference_transcripts(rows):
    """Return human-readable reasons if any row would pull transcripts into a bundle."""
    problems = []
    for index, row in enumerate(rows, start=1):
        for key in ("transcript_path", "transcript", "transcripts_dir"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                problems.append(
                    f"row {index} ({row.get('run_id', '?')}): field {key!r} "
                    f"points at {value!r} — transcripts are LOCAL-ONLY and "
                    "must never be bundled"
                )
        for key in ("full_output",):
            value = row.get(key)
            if isinstance(value, str) and len(value) > 200:
                problems.append(
                    f"row {index} ({row.get('run_id', '?')}): field {key!r} "
                    "looks like a transcript body — refusing to publish"
                )
    return problems


def find_candidate_gate_record(candidate_name, search_dirs=None):
    """Return True when a PASS gate archive mentioning ``candidate_name`` exists."""
    roots = list(search_dirs or [])
    if not roots:
        cwd = os.getcwd()
        roots = [
            os.path.join(cwd, "data"),
            os.path.join(cwd, ".openbench", "gate"),
            os.path.join(cwd, "results", "gate"),
        ]
    needles = {candidate_name, f'"candidate": "{candidate_name}"',
               f'"candidate":"{candidate_name}"'}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith((".json", ".jsonl", ".txt", ".md")):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                if "PASS" not in text:
                    continue
                if any(needle in text for needle in needles):
                    return path
    return None


def unmatched_arm_warnings(rows):
    """Warn when arms disagree on task set, model, or trial counts."""
    by_arm = defaultdict(list)
    for row in rows:
        if not stats.is_valid_result_row(row):
            continue
        kind = "candidate" if _is_candidate_row(row) else "baseline"
        label = compare._row_arm(row)
        # Mirror report_page / compare labeling for candidate vs stock name clash.
        baseline_names = {
            compare._row_arm(r) for r in rows
            if stats.is_valid_result_row(r) and not _is_candidate_row(r)
        }
        if kind == "candidate" and label in baseline_names:
            display = f"{label} (candidate)"
        else:
            display = label
        by_arm[display].append(row)

    if len(by_arm) < 2:
        return []

    warnings = []
    task_sets = {arm: frozenset(r["task"] for r in arm_rows)
                 for arm, arm_rows in by_arm.items()}
    model_sets = {arm: frozenset(str(r.get("model")) for r in arm_rows)
                  for arm, arm_rows in by_arm.items()}
    trial_maps = {}
    for arm, arm_rows in by_arm.items():
        counts = defaultdict(set)
        for row in arm_rows:
            counts[row["task"]].add(int(row["trial"]))
        trial_maps[arm] = {task: frozenset(trials) for task, trials in counts.items()}

    # Matched-cell coverage (reuse compare unique-cell logic).
    cells_by_arm = {}
    for arm, arm_rows in by_arm.items():
        unique, _dup = compare._unique_cells(arm_rows)
        cells_by_arm[arm] = set(unique)
    common = set.intersection(*cells_by_arm.values()) if cells_by_arm else set()
    for arm, cells in cells_by_arm.items():
        extra = len(cells - common)
        if extra:
            warnings.append(
                f"arm {arm!r} has {extra} (task, trial) cell(s) not shared with "
                f"every other arm (matched denominator would drop them)"
            )

    unique_task_sets = set(task_sets.values())
    if len(unique_task_sets) > 1:
        detail = "; ".join(
            f"{arm}={sorted(tasks)}" for arm, tasks in sorted(task_sets.items())
        )
        warnings.append(f"arms use different task sets: {detail}")

    unique_model_sets = set(model_sets.values())
    if len(unique_model_sets) > 1:
        detail = "; ".join(
            f"{arm}={sorted(models)}" for arm, models in sorted(model_sets.items())
        )
        warnings.append(f"arms use different models: {detail}")

    # Trial-count mismatch on shared tasks.
    shared_tasks = set.intersection(*(set(m) for m in trial_maps.values())) if trial_maps else set()
    for task in sorted(shared_tasks):
        trial_sets = {arm: trial_maps[arm].get(task, frozenset()) for arm in trial_maps}
        if len(set(trial_sets.values())) > 1:
            detail = "; ".join(
                f"{arm}=n_trials={len(trials)}" for arm, trials in sorted(trial_sets.items())
            )
            warnings.append(f"task {task!r} has mismatched trial counts across arms: {detail}")

    return warnings


def gate_missing_warnings(rows, search_dirs=None):
    """Warn when candidate arms lack an archived admission-gate PASS record."""
    warnings = []
    seen = set()
    for row in rows:
        if not _is_candidate_row(row):
            continue
        name = _candidate_name(row)
        if not name or name in seen:
            continue
        seen.add(name)
        if find_candidate_gate_record(name, search_dirs=search_dirs) is None:
            warnings.append(
                f"candidate {name!r} has no candidate-gate PASS record under "
                "data/, .openbench/gate/, or results/gate/ — run "
                f"`obench gate <spec> --model ...` (and archive the JSON) "
                "before treating this claim as admission-ready"
            )
    return warnings


def filter_publish_pii(report):
    """Keep only high-signal PII categories (drop digest-like hex/base64 hits)."""
    filtered = {}
    for rel, hits in report.items():
        kept = [hit for hit in hits if hit[0] in PUBLISH_PII_CATEGORIES]
        if kept:
            filtered[rel] = kept
    return filtered


def check_bundle_pii(bundle_dir, ctx=None):
    """Run scrub --check logic on bundle files; return filtered hit report."""
    ctx = ctx or scrub.build_context()
    return filter_publish_pii(scrub.check_tree(bundle_dir, ctx))


def write_results_jsonl(path, rows):
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                      for row in rows)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return payload.encode("utf-8")


def build_provenance(rows, results_sha256, tasks_dirs=None, warnings=None,
                     highlight=None, obench_version=None):
    """Machine-readable provenance for the published bundle."""
    roots = stats.parse_tasks_dirs(tasks_dirs)
    arms = {}
    tasks = {}
    models = sorted({str(row.get("model")) for row in rows})
    trial_counts = defaultdict(set)
    run_dates = []

    for row in rows:
        kind = "candidate" if _is_candidate_row(row) else "baseline"
        name = _arm_name(row)
        key = f"{kind}:{name}"
        if key not in arms:
            entry = {
                "kind": kind,
                "name": name,
                "harness": row.get("harness"),
                "harness_version": row.get("harness_version"),
            }
            if kind == "candidate":
                entry["candidate_provenance"] = row.get("candidate_provenance")
                prov = row.get("candidate_provenance") or {}
                entry["identity_digest"] = (
                    prov.get("candidate_digest") or prov.get("identity_digest")
                )
            else:
                entry["candidate_provenance"] = None
                entry["identity_digest"] = None
            arms[key] = entry

        task = row.get("task")
        if task and task not in tasks:
            task_dir = resolve_task_dir(task, roots)
            digest = None
            if task_dir is not None:
                try:
                    digest = task_content_digest(
                        task_dir, scheme=DIGEST_SCHEME_CURRENT
                    )
                except PublishError:
                    digest = None
            tasks[task] = {
                "task": task,
                "content_digest": digest,
                "workspace_source_samples": [],
            }
        if task and isinstance(row.get("workspace_source"), dict):
            sample = sanitize_row_for_publish({"workspace_source": row["workspace_source"]})[
                "workspace_source"
            ]
            if sample not in tasks[task]["workspace_source_samples"]:
                tasks[task]["workspace_source_samples"].append(sample)

        trial_counts[(row.get("harness"), row.get("task"), row.get("model"))].add(
            int(row["trial"]) if row.get("trial") is not None else 0
        )
        for field in ("started_at", "finished_at", "timestamp", "run_at"):
            value = row.get(field)
            if isinstance(value, str) and value:
                run_dates.append(value)

    trial_summary = {
        f"{h}/{t}/{m}": sorted(trials)
        for (h, t, m), trials in sorted(trial_counts.items())
    }
    return {
        "obench_version": obench_version or __version__,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "results_sha256": results_sha256,
        "digest_scheme": DIGEST_SCHEME_CURRENT,
        "arms": [arms[key] for key in sorted(arms)],
        "tasks": [tasks[name] for name in sorted(tasks)],
        "models": models,
        "trial_counts": trial_summary,
        "run_dates": sorted(set(run_dates)),
        "highlight_arms": list(highlight or []),
        "warnings": list(warnings or []),
    }


def default_out_dir(name=None):
    stamp = name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stamp).strip("-") or "bundle"
    return os.path.join(os.getcwd(), "openbench-publish", safe)


def render_publish_card(models, *, title, headline, highlight_arms, warnings,
                        methodology, meta):
    """Self-contained comparison card built on report_page helpers."""
    # Prefer report_page.render_page (charts + highlighting) then inject a
    # publish-specific headline metrics table with mean score / mean wall.
    page = report_page.render_page(
        models,
        methodology,
        title=title,
        headline=headline,
        highlight_arms=highlight_arms,
        banner_warnings=warnings,
    )
    # Insert a compact metrics table after the header for the show-off card.
    metrics = _publish_metrics_section(models, highlight_arms, meta)
    marker = "</header>"
    if marker in page:
        page = page.replace(marker, marker + metrics, 1)
    return page


def _publish_metrics_section(models, highlight_arms, meta):
    highlight = {str(name) for name in (highlight_arms or [])}
    heads = ["Arm", "Solved/n", "Solve rate", "Wilson 95% CI", "Mean score",
             "Mean wall", "Tokens/solve", "Token basis"]
    rows_html = []
    for model in models:
        for arm in model["arms"]:
            wilson = arm.get("wilson") or (None, None)
            if wilson[0] is None:
                wilson_s = "—"
            else:
                wilson_s = f"{wilson[0] * 100:.1f}–{wilson[1] * 100:.1f}%"
            basis = arm.get("token_basis") or "—"
            values = [
                f"{arm['arm']} × {model['model']}",
                f"{arm['solved']}/{arm['n']}",
                report_page._pct(arm.get("rate")),
                wilson_s,
                report_page._num(arm.get("mean_score"), 3),
                (report_page._num(arm.get("mean_wall"), 1) + "s"
                 if arm.get("mean_wall") is not None else "—"),
                report_page._num(arm.get("total_tokens")),
                basis,
            ]
            css = ' class="highlight"' if arm["arm"] in highlight else ""
            cells = "".join(f"<td>{html.escape(str(v))}</td>" for v in values)
            rows_html.append(f"<tr{css} data-arm=\"{html.escape(arm['arm'], quote=True)}\">"
                             f"{cells}</tr>")
    head = "".join(f"<th>{html.escape(h)}</th>" for h in heads)
    meta_bits = [
        f"obench {html.escape(str(meta.get('obench_version', '')))}",
        f"tasks: {html.escape(', '.join(meta.get('tasks') or []) or '—')}",
        f"models: {html.escape(', '.join(meta.get('models') or []) or '—')}",
        f"trials: {html.escape(str(meta.get('trial_summary') or '—'))}",
    ]
    if meta.get("run_dates"):
        meta_bits.append("dates: " + html.escape(", ".join(meta["run_dates"])))
    basis_note = (
        '<p class="table-note token-note">Tokens/solve uses fresh totals: '
        'self-reported <code>tokens</code> when present, else proxy-measured '
        '(uncached input + output). Badge: unmetered / self-reported / '
        'proxy-measured. Cache-read is not folded into Tokens/solve.</p>'
    )
    return (
        '<section class="publish-card"><h2>Comparison card</h2>'
        '<p class="tag">' + " · ".join(meta_bits) + "</p>"
        '<div class="scroll"><table class="results"><thead><tr>'
        + head + "</tr></thead><tbody>" + "".join(rows_html)
        + "</tbody></table></div>" + basis_note + "</section>"
    )


def bundle_readme(provenance):
    warnings = provenance.get("warnings") or []
    warning_block = ""
    if warnings:
        warning_block = "\n## Comparability warnings\n\n" + "\n".join(
            f"- {w}" for w in warnings
        ) + "\n"
    return f"""# OpenBench publish bundle

This directory is a **shareable comparison artifact** produced by
`obench publish`. It is meant to be posted as evidence that a candidate
harness was run against OpenBench stock arms.

## Contents

| File | Purpose |
|------|---------|
| `index.html` | Self-contained HTML comparison card (candidate arms highlighted) |
| `results.jsonl` | Filtered result rows included in the claim (no transcripts) |
| `provenance.json` | Machine-readable digests + arm identities + results SHA-256 |
| `README.md` | This file |

## Re-verify

```bash
obench verify .
```

`obench verify` recomputes the SHA-256 of `results.jsonl` and, when task
trees are available locally, per-task content digests recorded in
`provenance.json`. Each check prints PASS or FAIL.

### What verify proves

- The results file has not been edited since publish (hash match).
- Task content digests still match the recorded instruction/checker/workspace
  hashes when those tasks are present on the verifying machine.

### What verify does NOT prove

- The runs were not cherry-picked or rerun until green.
- Live candidate-gate checks succeeded on the publisher's machine.
- Transcripts were reviewed (they are **never** included — LOCAL-ONLY).

Publish the full matrix whenever possible.
{warning_block}
Generated by obench {provenance.get("obench_version", "?")} at
{provenance.get("generated_at", "?")}.
"""


def create_bundle(results_path, out_dir, *, candidate_specs=None, tasks_dirs=None,
                  title=None, allow_pii_override=False, gate_search_dirs=None,
                  scrub_ctx=None):
    """Build a publish bundle under ``out_dir``. Returns provenance dict."""
    results_path = os.path.abspath(results_path)
    if not os.path.isfile(results_path):
        raise PublishError(f"results not found: {results_path}")

    rows = stats.load_rows([results_path])
    if not rows:
        raise PublishError(f"no result rows in {results_path}")
    invalid = sum(not stats.is_valid_result_row(row) for row in rows)
    if invalid:
        raise PublishError(
            f"{results_path}: {invalid} invalid result row(s); refusing to publish"
        )

    transcript_problems = rows_reference_transcripts(rows)
    if transcript_problems:
        raise PublishError(
            "refusing to publish: transcript material would be bundled:\n  "
            + "\n  ".join(transcript_problems)
        )

    # Never copy a sibling transcripts/ tree into the out dir.
    sibling_transcripts = os.path.join(
        os.path.dirname(results_path), "transcripts"
    )
    if os.path.isdir(sibling_transcripts) and os.path.abspath(out_dir).startswith(
            os.path.abspath(sibling_transcripts) + os.sep):
        raise PublishError("refusing to publish into a transcripts/ directory")

    highlight = resolve_highlight_names(candidate_specs)
    # Auto-highlight every candidate arm when none named.
    if not highlight:
        highlight = sorted({
            _candidate_name(row) for row in rows
            if _candidate_name(row)
        })

    publish_rows = [sanitize_row_for_publish(row) for row in rows]
    warnings = unmatched_arm_warnings(publish_rows)
    warnings.extend(gate_missing_warnings(publish_rows, search_dirs=gate_search_dirs))

    if tasks_dirs is None:
        discovered = default_tasks_dir()
        tasks_dirs = [discovered] if discovered else []

    os.makedirs(out_dir, exist_ok=True)
    # Refuse if caller somehow pointed --out at transcripts.
    if os.path.basename(os.path.abspath(out_dir)) == "transcripts":
        raise PublishError("refusing to use transcripts/ as --out")

    results_out = os.path.join(out_dir, "results.jsonl")
    raw = write_results_jsonl(results_out, publish_rows)
    results_sha = _sha256_bytes(raw)

    provenance = build_provenance(
        publish_rows,
        results_sha,
        tasks_dirs=tasks_dirs,
        warnings=warnings,
        highlight=highlight,
        obench_version=__version__,
    )
    provenance_path = os.path.join(out_dir, "provenance.json")
    with open(provenance_path, "w", encoding="utf-8") as fh:
        json.dump(provenance, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")

    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(bundle_readme(provenance))

    # Assemble HTML via report_page machinery.
    tmp_results = results_out
    models = report_page.assemble_tables(
        [{"path": tmp_results, "matched": False}],
        tasks_dirs=tasks_dirs or [os.getcwd()],
    )
    task_names = sorted({row["task"] for row in publish_rows})
    trial_ns = sorted({
        len(v) for v in provenance.get("trial_counts", {}).values()
    }) or ["?"]
    meta = {
        "obench_version": __version__,
        "tasks": task_names,
        "models": provenance.get("models") or [],
        "trial_summary": (
            f"{min(trial_ns)}–{max(trial_ns)}" if len(trial_ns) > 1
            else str(trial_ns[0])
        ),
        "run_dates": provenance.get("run_dates") or [],
    }
    # Map highlight names onto report_page arm labels (may include " (candidate)").
    arm_labels = set()
    for model in models:
        for arm in model["arms"]:
            arm_labels.add(arm["arm"])
    highlight_labels = []
    for name in highlight:
        if name in arm_labels:
            highlight_labels.append(name)
        elif f"{name} (candidate)" in arm_labels:
            highlight_labels.append(f"{name} (candidate)")
        else:
            # Partial match: any label that starts with the candidate name.
            highlight_labels.extend(
                label for label in arm_labels
                if label == name or label.startswith(name + " ")
            )

    card_title = title or "OpenBench comparison"
    headline = (
        f"Candidate arm(s) highlighted vs stock · {len(publish_rows)} rows · "
        f"obench {__version__}"
    )
    page = render_publish_card(
        models,
        title=card_title,
        headline=headline,
        highlight_arms=highlight_labels,
        warnings=warnings,
        methodology=PUBLISH_METHODOLOGY,
        meta=meta,
    )
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)

    # Safety: ensure we did not create a transcripts/ directory in the bundle.
    if os.path.isdir(os.path.join(out_dir, "transcripts")):
        shutil.rmtree(os.path.join(out_dir, "transcripts"), ignore_errors=True)
        raise PublishError("internal error: transcripts/ appeared in the bundle")

    pii_report = check_bundle_pii(out_dir, ctx=scrub_ctx)
    if pii_report:
        total = sum(len(v) for v in pii_report.values())
        scrub._print_check_report(out_dir, pii_report)
        message = (
            f"PII sanity check found {total} potential hit(s) in the bundle. "
            "Refuse to publish by default. Re-run with --allow-pii-override "
            "only after manual review (dangerous)."
        )
        if not allow_pii_override:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise PublishError(message)
        print(f"WARNING: {message}", file=sys.stderr)
        print("(proceeding because --allow-pii-override was set)", file=sys.stderr)

    return provenance


def verify_bundle(bundle_dir, tasks_dirs=None):
    """Recompute digests; return list of {name, status, detail} checks."""
    bundle_dir = os.path.abspath(bundle_dir)
    checks = []
    provenance_path = os.path.join(bundle_dir, "provenance.json")
    results_path = os.path.join(bundle_dir, "results.jsonl")

    if not os.path.isfile(provenance_path):
        return [{"name": "provenance.json", "status": "FAIL",
                 "detail": "missing provenance.json"}]
    if not os.path.isfile(results_path):
        return [{"name": "results.jsonl", "status": "FAIL",
                 "detail": "missing results.jsonl"}]

    with open(provenance_path, encoding="utf-8") as fh:
        provenance = json.load(fh)

    expected = provenance.get("results_sha256")
    actual = _sha256_file(results_path)
    checks.append({
        "name": "results_sha256",
        "status": "PASS" if expected and expected == actual else "FAIL",
        "detail": f"expected={expected} actual={actual}",
    })

    try:
        digest_scheme = resolve_digest_scheme(provenance)
    except PublishError as exc:
        checks.append({
            "name": "digest_scheme",
            "status": "FAIL",
            "detail": str(exc),
        })
        return checks

    if tasks_dirs is None:
        discovered = default_tasks_dir()
        tasks_dirs = [discovered] if discovered else []
    roots = stats.parse_tasks_dirs(tasks_dirs) if tasks_dirs else []

    for task_entry in provenance.get("tasks") or []:
        task = task_entry.get("task")
        expected_digest = task_entry.get("content_digest")
        name = f"task_digest:{task}"
        if not expected_digest:
            checks.append({
                "name": name,
                "status": "FAIL",
                "detail": "no content_digest recorded at publish time; "
                          "cannot verify task fingerprint",
            })
            continue
        task_dir = resolve_task_dir(task, roots) if roots else None
        if task_dir is None:
            checks.append({
                "name": name,
                "status": "FAIL",
                "detail": f"task tree not found for {task!r}; cannot recompute digest",
            })
            continue
        try:
            actual_digest = task_content_digest(task_dir, scheme=digest_scheme)
        except PublishError as exc:
            checks.append({"name": name, "status": "FAIL", "detail": str(exc)})
            continue
        checks.append({
            "name": name,
            "status": "PASS" if actual_digest == expected_digest else "FAIL",
            "detail": (
                f"scheme={digest_scheme} expected={expected_digest} "
                f"actual={actual_digest}"
            ),
        })

    return checks


def print_verify_report(checks):
    failed = 0
    for item in checks:
        print(f"{item['status']}: {item['name']} — {item['detail']}")
        if item["status"] != "PASS":
            failed += 1
    verdict = "FAIL" if failed else "PASS"
    print(f"VERDICT: {verdict} ({len(checks) - failed}/{len(checks)} checks passed)")
    return 0 if failed == 0 else 1


def _publish_main(argv):
    parser = argparse.ArgumentParser(
        prog="obench publish",
        description="Build a shareable comparison bundle (HTML + provenance).",
    )
    parser.add_argument(
        "--results-path", default=None,
        help="results JSONL (default: same resolution as run/report)",
    )
    parser.add_argument(
        "--candidate", action="append", default=[],
        help="candidate name or spec path to highlight (repeatable)",
    )
    parser.add_argument(
        "--out", default=None,
        help="output bundle directory (default: ./openbench-publish/<timestamp>/)",
    )
    parser.add_argument(
        "--name", default=None,
        help="bundle directory name under openbench-publish/ (ignored if --out set)",
    )
    parser.add_argument(
        "--tasks-dir", action="append", default=None,
        help="task root for content digests (repeatable)",
    )
    parser.add_argument("--title", default=None, help="HTML card title")
    parser.add_argument(
        "--allow-pii-override", action="store_true",
        help="DANGEROUS: publish even when the PII sanity check finds hits",
    )
    args = parser.parse_args(argv)

    results_path = args.results_path
    if results_path is None:
        from .config import load_config
        cfg = load_config()
        results_path = cfg.results_path or default_results_path()

    out_dir = os.path.abspath(args.out) if args.out else default_out_dir(args.name)
    tasks_dirs = args.tasks_dir
    if not tasks_dirs:
        try:
            tasks_dirs = [resolve_tasks_dir()]
        except Exception:  # noqa: BLE001 - publish can proceed without tasks
            tasks_dirs = []

    try:
        provenance = create_bundle(
            results_path,
            out_dir,
            candidate_specs=args.candidate,
            tasks_dirs=tasks_dirs,
            title=args.title,
            allow_pii_override=args.allow_pii_override,
        )
    except PublishError as exc:
        print(f"publish: {exc}", file=sys.stderr)
        return 2

    for warning in provenance.get("warnings") or []:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"Published bundle → {out_dir}")
    print(f"  index.html      comparison card")
    print(f"  results.jsonl   {len(stats.load_rows([os.path.join(out_dir, 'results.jsonl')]))} rows")
    print(f"  provenance.json results_sha256={provenance['results_sha256'][:16]}…")
    print(f"  README.md       re-verify with: obench verify {out_dir}")
    return 0


def _verify_main(argv):
    parser = argparse.ArgumentParser(
        prog="obench verify",
        description="Re-verify an OpenBench publish bundle's digests.",
    )
    parser.add_argument("bundle_dir", help="path to a publish bundle directory")
    parser.add_argument(
        "--tasks-dir", action="append", default=None,
        help="task root for recomputing content digests (repeatable)",
    )
    args = parser.parse_args(argv)

    tasks_dirs = args.tasks_dir
    if not tasks_dirs:
        try:
            tasks_dirs = [resolve_tasks_dir()]
        except Exception:  # noqa: BLE001
            tasks_dirs = []

    checks = verify_bundle(args.bundle_dir, tasks_dirs=tasks_dirs)
    return print_verify_report(checks)


def main(argv=None):
    """Entry used when invoked as ``python -m obench.publish`` (publish only)."""
    return _publish_main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
