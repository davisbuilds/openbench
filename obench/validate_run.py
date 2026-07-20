#!/usr/bin/env python3
"""Post-run validation gate for benchmark JSONL results.

The gate is intentionally read-only: it inspects a results JSONL and optional
LOCAL-ONLY transcripts, then exits 0 for PASS or 3 when any finding requires a
rerun, reclassification, or investigation.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

from .failure_class import FAILURE_CLASSES, has_instant_cli_exit_shape

EXIT_FINDINGS = 3
DEFAULT_TIMEOUT_S = 600.0
STRONG_VENDOR_MARKER_RE = re.compile(
    r"HTTP\s*429|insufficient balance|\[1113\]",
    re.IGNORECASE,
)
CONTEXTUAL_VENDOR_MARKER_RE = re.compile(
    r"(?:API|provider|vendor|HTTP|status|response|error)[^\n]{0,200}"
    r"(?:rate.?limit\w*\s+exceed\w*|quota)|"
    r"(?:rate.?limit\w*\s+exceed\w*|quota)[^\n]{0,200}"
    r"(?:API|provider|vendor|HTTP|status|response|error)",
    re.IGNORECASE,
)
EXIT_137_RE = re.compile(r"\bexit[- ]137\b", re.IGNORECASE)
REQUIRED_FIELDS = {
    "run_id", "harness", "model", "task", "trial", "success",
    "completed", "wall_time_s", "checker_exit", "failure_class",
}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _safe_run_id(row):
    rid = row.get("run_id")
    if rid:
        return str(rid)
    harness = row.get("harness")
    task = row.get("task")
    model = row.get("model")
    trial = row.get("trial")
    if harness is not None and task is not None and model is not None and trial is not None:
        return make_run_id(harness, task, model, trial)
    return "<unknown>"


def make_run_id(harness, task, model, trial):
    return f"{harness}:{task}:{model}:trial{trial}"


def _cell_key(row):
    return (row.get("harness"), row.get("model"), row.get("task"), row.get("trial"))


def is_structurally_valid_row(row):
    if "_invalid_json" in row:
        return False
    if any(field not in row for field in REQUIRED_FIELDS):
        return False
    if not all(isinstance(row.get(field), str) and row.get(field)
               for field in ("run_id", "harness", "model", "task")):
        return False
    if not isinstance(row.get("trial"), int) or isinstance(row.get("trial"), bool):
        return False
    if not isinstance(row.get("success"), bool):
        return False
    if not isinstance(row.get("completed"), bool):
        return False
    wall = row.get("wall_time_s")
    if wall is not None and not _is_number(wall):
        return False
    return True


def load_rows(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                row = {"run_id": f"{path}:{lineno}", "_invalid_json": str(exc)}
            if not isinstance(row, dict):
                row = {"run_id": f"{path}:{lineno}", "_invalid_json": "row is not an object"}
            row["_lineno"] = lineno
            rows.append(row)
    return rows


def _split_expect_item(item):
    """Parse one --expect item as harness or harness/model pair.

    Plain harness names are crossed with all models observed in the file. Model
    specific forms are accepted as ``harness=model`` or ``harness@model``; the
    delimiter choices avoid ambiguity with run_id's colon-separated shape.
    """
    item = item.strip()
    for delim in ("=", "@"):
        if delim in item:
            harness, model = item.split(delim, 1)
            return harness.strip(), model.strip()
    return item, None


def expected_harness_models(rows, expect_arg):
    observed = sorted({(r.get("harness"), r.get("model")) for r in rows
                       if r.get("harness") is not None and r.get("model") is not None})
    if not expect_arg:
        return observed

    models = sorted({model for _, model in observed})
    expected = []
    seen = set()
    for raw in expect_arg.split(","):
        if not raw.strip():
            continue
        harness, model = _split_expect_item(raw)
        if not harness:
            continue
        pairs = [(harness, model)] if model is not None else [(harness, m) for m in models]
        for pair in pairs:
            if pair not in seen:
                expected.append(pair)
                seen.add(pair)
    return expected


def finding(rule, run_ids, action, message, **extra):
    payload = {
        "rule": rule,
        "run_ids": sorted(str(r) for r in run_ids),
        "suggested_action": action,
        "message": message,
    }
    payload.update(extra)
    return payload


def info(rule, run_ids, message, **extra):
    return finding(rule, run_ids, "none", message, level="info", **extra)


def check_completeness(rows, expect_arg=None):
    findings = []
    valid_rows = [row for row in rows if is_structurally_valid_row(row)]
    if not valid_rows:
        return [finding(
            "completeness.empty",
            [],
            "rerun",
            "results file contains no valid result rows",
        )]

    counts = Counter(_cell_key(row) for row in valid_rows)
    by_key = defaultdict(list)
    for row in valid_rows:
        by_key[_cell_key(row)].append(_safe_run_id(row))

    for key, count in sorted(counts.items(), key=lambda item: tuple(str(part) for part in item[0])):
        if count > 1:
            findings.append(finding(
                "completeness.duplicate",
                by_key[key],
                "investigate",
                f"cell appears {count} times: harness={key[0]} model={key[1]} task={key[2]} trial={key[3]}",
            ))

    if expect_arg:
        tasks = sorted({r.get("task") for r in valid_rows if r.get("task") is not None})
        trials = sorted({r.get("trial") for r in valid_rows if r.get("trial") is not None})
        expected_pairs = expected_harness_models(valid_rows, expect_arg)
        for harness, model in expected_pairs:
            for task in tasks:
                for trial in trials:
                    key = (harness, model, task, trial)
                    if counts.get(key, 0) == 0:
                        rid = make_run_id(harness, task, model, trial)
                        findings.append(finding(
                            "completeness.missing",
                            [rid],
                            "rerun",
                            "expected cell is missing",
                            expected_cell={"harness": harness, "model": model, "task": task, "trial": trial},
                        ))
    return findings


def check_taxonomy(rows):
    findings = []
    for row in rows:
        if "_invalid_json" in row:
            continue
        rid = _safe_run_id(row)
        fc = row.get("failure_class")
        success = bool(row.get("success"))
        if fc not in FAILURE_CLASSES:
            findings.append(finding(
                "taxonomy.unknown_failure_class",
                [rid],
                "reclassify",
                f"unknown failure_class={fc!r}",
            ))
            continue
        if fc == "rate_limited":
            findings.append(finding(
                "taxonomy.rate_limited",
                [rid],
                "rerun",
                "rate_limited rows are not valid benchmark outcomes",
            ))
        if bool(row.get("completed")) and fc == "timeout":
            findings.append(finding(
                "taxonomy.completed_timeout",
                [rid],
                "reclassify",
                "completed=True rows must not be classified timeout",
            ))
        if success and fc != "solved":
            findings.append(finding(
                "taxonomy.success_not_solved",
                [rid],
                "reclassify",
                "success=True rows must have failure_class=solved",
            ))
        if not success and fc == "solved":
            findings.append(finding(
                "taxonomy.failure_marked_solved",
                [rid],
                "reclassify",
                "success=False rows must not have failure_class=solved",
            ))
    return findings


def sanitize_run_id(run_id):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(run_id))


def _task_filename_part(task):
    return str(task or "").replace("/", "_")


def _safe_component(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))


def _inside_root(path, root):
    try:
        return os.path.commonpath([os.path.realpath(root), os.path.realpath(path)]) == os.path.realpath(root)
    except ValueError:
        return False


def _candidate_under_root(path, root):
    real_path = os.path.realpath(path)
    return real_path if _inside_root(real_path, root) else None


def transcript_candidates(row, results_path, transcripts_dir):
    if not transcripts_dir:
        return []
    base = os.path.realpath(transcripts_dir)
    stem = os.path.splitext(os.path.basename(results_path))[0]
    raw_candidates = []
    explicit = row.get("transcript_path")
    if explicit:
        explicit_path = explicit if os.path.isabs(explicit) else os.path.join(base, explicit)
        raw_candidates.append(explicit_path)
    rid = row.get("run_id")
    if rid:
        safe = sanitize_run_id(rid) + ".txt"
        raw_candidates.extend([os.path.join(base, stem, safe), os.path.join(base, safe)])
    harness = row.get("harness")
    task = _task_filename_part(row.get("task"))
    model = row.get("model")
    trial = row.get("trial")
    if harness and task and model and trial is not None:
        legacy = "{}_{}_{}_trial{}.txt".format(
            _safe_component(harness),
            _safe_component(task),
            _safe_component(model),
            _safe_component(trial),
        )
        raw_candidates.extend([
            os.path.join(base, stem, legacy),
            os.path.join(base, legacy),
            os.path.join(base, f"tb-open-n3-{_safe_component(model)}", legacy),
        ])
    return [path for path in (_candidate_under_root(p, base) for p in raw_candidates) if path]


def find_transcript(row, results_path, transcripts_dir):
    for candidate in transcript_candidates(row, results_path, transcripts_dir):
        if os.path.isfile(candidate):
            return candidate
    return None


def check_contamination(rows, results_path, transcripts_dir):
    findings = []
    if not transcripts_dir:
        return findings
    for row in rows:
        if bool(row.get("success")):
            continue
        path = find_transcript(row, results_path, transcripts_dir)
        if not path:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        hits = (len(STRONG_VENDOR_MARKER_RE.findall(text)) +
                len(CONTEXTUAL_VENDOR_MARKER_RE.findall(text)))
        if hits >= 3:
            findings.append(finding(
                "contamination.vendor_markers",
                [_safe_run_id(row)],
                "investigate",
                f"failure transcript contains {hits} vendor/provider limit markers",
                transcript=path,
                marker_count=hits,
            ))
    return findings


def _timeout_cap(row):
    for key in ("timeout_s", "timeout"):
        value = row.get(key)
        if _is_number(value) and value > 0:
            return float(value)
    return DEFAULT_TIMEOUT_S


def _has_exit_137(row):
    if row.get("checker_exit") == 137:
        return True
    parts = [row.get("error"), row.get("checker_exit"), row.get("output_tail")]
    return bool(EXIT_137_RE.search("\n".join(str(p) for p in parts if p is not None)))


def check_telemetry(rows):
    findings = []
    for row in rows:
        rid = _safe_run_id(row)
        if bool(row.get("success")) and row.get("tokens_fresh") is None and row.get("tokens") is None:
            findings.append(finding(
                "telemetry.solved_missing_tokens",
                [rid],
                "investigate",
                "solved row is missing both tokens_fresh and tokens",
            ))
        if _has_exit_137(row):
            wall = row.get("wall_time_s")
            cap = _timeout_cap(row)
            if _is_number(wall) and float(wall) < cap * 0.98:
                findings.append(finding(
                    "telemetry.oom_exit_137",
                    [rid],
                    "investigate",
                    "under-cap exit 137 indicates likely OOM",
                    wall_time_s=wall,
                    timeout_cap_s=cap,
                ))
        if row.get("turns") == 1:
            tokens = row.get("tokens")
            if _is_number(tokens) and tokens > 50000:
                findings.append(finding(
                    "telemetry.single_turn_huge_tokens",
                    [rid],
                    "investigate",
                    "turns==1 with tokens>50000 is suspicious telemetry",
                    tokens=tokens,
                ))
    return findings


def check_instant_fails(rows):
    findings = []
    for row in rows:
        if has_instant_cli_exit_shape(row) and row.get("failure_class") != "infra":
            findings.append(finding(
                "instant_fails.classifier_drift",
                [_safe_run_id(row)],
                "reclassify",
                "near-instant bare CLI exit with no tokens should be classified infra",
            ))
    return findings


def check_unauditable_rows(rows):
    infos = []
    for row in rows:
        if "_invalid_json" in row:
            continue
        checker_stdout = row.get("checker_stdout")
        missing_stdout = (checker_stdout is None or
                          (isinstance(checker_stdout, str) and not checker_stdout.strip()))
        if (not bool(row.get("success")) and
                row.get("failure_class") == "wrong_answer" and
                missing_stdout):
            infos.append(info(
                "unauditable.missing_checker_stdout",
                [_safe_run_id(row)],
                "wrong_answer row is missing checker_stdout evidence",
            ))
    return infos


def validate(results_path, transcripts_dir=None, expect_arg=None):
    rows = load_rows(results_path)
    findings = []
    for row in rows:
        if "_invalid_json" in row:
            findings.append(finding(
                "json.invalid_row",
                [_safe_run_id(row)],
                "investigate",
                row["_invalid_json"],
            ))
        elif not is_structurally_valid_row(row):
            missing = sorted(field for field in REQUIRED_FIELDS if field not in row)
            findings.append(finding(
                "schema.invalid_row",
                [_safe_run_id(row)],
                "investigate",
                "row is missing required fields or has invalid field types",
                missing_fields=missing,
            ))
    findings.extend(check_completeness(rows, expect_arg))
    findings.extend(check_taxonomy(rows))
    findings.extend(check_contamination(rows, results_path, transcripts_dir))
    findings.extend(check_telemetry(rows))
    findings.extend(check_instant_fails(rows))
    infos = check_unauditable_rows(rows)
    return {
        "status": "FAIL" if findings else "PASS",
        "pass": not findings,
        "results": results_path,
        "rows": len(rows),
        "findings_count": len(findings),
        "findings": findings,
        "infos_count": len(infos),
        "infos": infos,
    }


def format_text(verdict):
    lines = [f"{verdict['status']} {verdict['results']} rows={verdict['rows']} findings={verdict['findings_count']}"]
    for item in verdict["findings"]:
        run_ids = ", ".join(item["run_ids"][:5])
        if len(item["run_ids"]) > 5:
            run_ids += f", ... (+{len(item['run_ids']) - 5})"
        lines.append(f"- {item['rule']} action={item['suggested_action']} run_ids=[{run_ids}] {item['message']}")
    for item in verdict.get("infos", []):
        run_ids = ", ".join(item["run_ids"][:5])
        if len(item["run_ids"]) > 5:
            run_ids += f", ... (+{len(item['run_ids']) - 5})"
        lines.append(f"- INFO {item['rule']} run_ids=[{run_ids}] {item['message']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a benchmark results JSONL after a run.")
    parser.add_argument("--results", required=True, help="results JSONL to validate")
    parser.add_argument("--transcripts-dir", default=None,
                        help="optional LOCAL-ONLY transcript root (default: transcripts/ sibling of results if present)")
    parser.add_argument("--expect", default=None,
                        help="comma-separated expected harnesses; use harness=model or harness@model for model-specific pairs")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON verdict")
    args = parser.parse_args(argv)

    transcripts_dir = args.transcripts_dir
    if transcripts_dir is None:
        default = os.path.join(os.path.dirname(os.path.abspath(args.results)), "transcripts")
        transcripts_dir = default if os.path.isdir(default) else None

    verdict = validate(args.results, transcripts_dir=transcripts_dir, expect_arg=args.expect)
    if args.json:
        print(json.dumps(verdict, indent=2, sort_keys=True))
    else:
        print(format_text(verdict))
    return 0 if verdict["pass"] else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
