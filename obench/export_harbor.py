#!/usr/bin/env python3
"""One-way exporter: OpenBench tasks → Harbor task directories.

Harbor (Apache-2.0) owns cloud sandboxes and containerized agent evals.
This module maps OpenBench's files-plus-checker contract onto Harbor's
``instruction.md`` + ``task.toml`` + ``environment/`` + ``tests/test.sh`` +
optional ``solution/solve.sh`` layout so companies can run OpenBench suites
on Harbor while keeping OpenBench as the comparison/stats layer.

Format pinned to Harbor 0.20.0 source at commit
72bc40b1e58b47a9cc6e0f14c29aced3a9e53767 (schema_version 1.4).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

from .paths import TasksDirError, resolve_tasks_dir
from .publish import DIGEST_SCHEME_CURRENT, PublishError, task_content_digest
from .validate_tasks import discover_tasks, effective_score, parse_score
from .workspace import (
    WorkspaceError,
    materialize_workspace,
    overlay_solution,
    resolve_workspace_mode,
)

# Harbor 0.20.0 task contract pinned at commit 72bc40b1e58b.
HARBOR_SCHEMA_VERSION = "1.4"
HARBOR_TASK_VERSION = "1.0.0"
HARBOR_WORKDIR = "/app"
DEFAULT_BASE_IMAGE = "python:3.11-slim"

# Reward log dir: Harbor uses /logs/verifier; local round-trips use a fallback.
REWARD_FILENAME = "reward.txt"
VERIFIER_EVIDENCE_FILENAME = "openbench-verifier-evidence.json"
VERIFIER_EVIDENCE_SCHEMA_VERSION = "openbench-verifier-evidence-v2"


class ExportError(ValueError):
    """User-facing export failure."""


def _toml_str(value: str) -> str:
    """Serialize a TOML basic string (escape minimal set)."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_str_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def _first_paragraph(text: str, limit: int = 240) -> str:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not parts:
        return ""
    first = " ".join(parts[0].split())
    first = re.sub(r"^#+\s*", "", first)
    if len(first) > limit:
        return first[: limit - 1].rstrip() + "…"
    return first


def map_checker_to_reward(exit_code: int, stdout: str) -> float:
    """Map OpenBench checker outcome to a Harbor reward float in [0, 1]."""
    return float(effective_score(exit_code, parse_score(stdout or "")))


def render_task_toml(
    *,
    task_name: str,
    description: str,
    workspace_provenance: dict | None,
    openbench_task_content_digest: str,
    tags: list[str] | None = None,
    network_mode: str = "no-network",
    agent_timeout_sec: float = 600.0,
    verifier_timeout_sec: float = 120.0,
) -> str:
    """Render Harbor ``task.toml`` (schema_version 1.4)."""
    if re.fullmatch(r"[0-9a-f]{64}", openbench_task_content_digest) is None:
        raise ExportError(
            "OpenBench task content digest must be 64 lowercase hex characters"
        )
    tags = list(tags) if tags else ["openbench"]
    keywords = ["openbench"]
    harbor_name = f"openbench/{task_name}"
    lines = [
        f"schema_version = {_toml_str(HARBOR_SCHEMA_VERSION)}",
        "artifacts = [",
        f"    {{ source = {_toml_str(HARBOR_WORKDIR)}, "
        f"destination = {_toml_str('workspace')} }},",
        "]",
        "",
        "[task]",
        f"name = {_toml_str(harbor_name)}",
        f"version = {_toml_str(HARBOR_TASK_VERSION)}",
        f"description = {_toml_str(description)}",
        "authors = []",
        f"keywords = {_toml_str_list(keywords)}",
        "",
        "[metadata]",
        f"origin = {_toml_str('openbench')}",
        f"difficulty = {_toml_str('unknown')}",
        f"category = {_toml_str('programming')}",
        f"tags = {_toml_str_list(tags)}",
        f"openbench_task = {_toml_str(task_name)}",
    ]
    if workspace_provenance:
        kind = workspace_provenance.get("kind", "git")
        lines.append(f"openbench_workspace_kind = {_toml_str(str(kind))}")
        if workspace_provenance.get("resolved_sha"):
            lines.append(
                "openbench_workspace_resolved_sha = "
                + _toml_str(str(workspace_provenance["resolved_sha"]))
            )
        if workspace_provenance.get("repo") is not None:
            lines.append(
                "openbench_workspace_repo = "
                + _toml_str(str(workspace_provenance["repo"]))
            )
        if workspace_provenance.get("ref") is not None:
            lines.append(
                "openbench_workspace_ref = "
                + _toml_str(str(workspace_provenance["ref"]))
            )
        if workspace_provenance.get("subdir"):
            lines.append(
                "openbench_workspace_subdir = "
                + _toml_str(str(workspace_provenance["subdir"]))
            )
    else:
        lines.append(f"openbench_workspace_kind = {_toml_str('snapshot')}")

    lines += [
        "",
        "[metadata.openbench_task_content_digest]",
        f"scheme = {DIGEST_SCHEME_CURRENT}",
        f"sha256 = {_toml_str(openbench_task_content_digest)}",
        "",
        "[verifier]",
        f"timeout_sec = {float(verifier_timeout_sec)}",
        "",
        "[agent]",
        f"timeout_sec = {float(agent_timeout_sec)}",
        "",
        "[environment]",
        "build_timeout_sec = 600.0",
        f"network_mode = {_toml_str(network_mode)}",
        f'os = {_toml_str("linux")}',
        "cpus = 1",
        "memory_mb = 2048",
        "storage_mb = 10240",
        "gpus = 0",
        "",
    ]
    return "\n".join(lines)


def render_dockerfile(*, base_image: str = DEFAULT_BASE_IMAGE) -> str:
    """Minimal agent environment: bash + python3, workspace at WORKDIR."""
    return (
        f"FROM {base_image}\n"
        "\n"
        "# OpenBench → Harbor export: materialized workspace lives under app/.\n"
        f"WORKDIR {HARBOR_WORKDIR}\n"
        "COPY app/ /app/\n"
    )


def render_test_sh(openbench_task_content_digest: str) -> str:
    """Harbor verifier wrapper around OpenBench ``checker.sh``.

    Writes scalar ``reward.txt`` plus machine-readable verifier evidence under
    Harbor's ``/logs/verifier``. For local round-trip tests without Harbor's
    filesystem layout, honors ``VERIFIER_LOGS_DIR`` and otherwise falls back to
    ``./logs-verifier`` relative to the agent workspace cwd.
    """
    if re.fullmatch(r"[0-9a-f]{64}", openbench_task_content_digest) is None:
        raise ExportError(
            "OpenBench task content digest must be 64 lowercase hex characters"
        )
    script = r"""#!/usr/bin/env bash
# OpenBench → Harbor verifier: run checker.sh, map exit/SCORE → reward.txt.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TASK_DIR="$TESTS_DIR"

# Prefer Harbor's /logs/verifier when present; else VERIFIER_LOGS_DIR or
# ./logs-verifier for local round-trip harnesses without a /logs mount.
if [ -n "${VERIFIER_LOGS_DIR:-}" ]; then
  REWARD_DIR="$VERIFIER_LOGS_DIR"
elif [ -d /logs/verifier ]; then
  REWARD_DIR="/logs/verifier"
else
  REWARD_DIR="$(pwd)/logs-verifier"
fi
mkdir -p "$REWARD_DIR"

START_EPOCH="$(date +%s 2>/dev/null || true)"
OUT_FILE="$(mktemp)"
set +e
bash "$TESTS_DIR/checker.sh" >"$OUT_FILE" 2>&1
RC=$?
set -e
cat "$OUT_FILE"

PARSED_SCORE="$(
  awk '
    /^[[:space:]]*SCORE:[[:space:]]*/ {
      line=$0
      sub(/^[[:space:]]*SCORE:[[:space:]]*/, "", line)
      split(line, a, /[[:space:]]+/)
      candidate=a[1]
      if (candidate ~ /^[-+]?(([0-9]+([.][0-9]*)?)|([.][0-9]+))([eE][-+]?[0-9]+)?$/) {
        value=candidate + 0
        if (value < 0) value=0
        if (value > 1) value=1
        last=sprintf("%.17g", value)
      }
    }
    END { if (last != "") print last }
  ' "$OUT_FILE"
)"

if [ "$RC" -eq 0 ]; then
  REWARD="1.0"
elif [ -n "$PARSED_SCORE" ]; then
  REWARD="$PARSED_SCORE"
else
  REWARD="0.0"
fi

printf '%s\n' "$REWARD" >"$REWARD_DIR/reward.txt"
END_EPOCH="$(date +%s 2>/dev/null || true)"
case "$START_EPOCH:$END_EPOCH" in
  :*|*:|*[!0-9:]*) DURATION_JSON="null" ;;
  *)
    if [ "$END_EPOCH" -ge "$START_EPOCH" ]; then
      DURATION_JSON="$((END_EPOCH - START_EPOCH))"
    else
      DURATION_JSON="null"
    fi
    ;;
esac
if [ -n "$PARSED_SCORE" ]; then
  PARSED_SCORE_JSON="$PARSED_SCORE"
else
  PARSED_SCORE_JSON="null"
fi
cat >"$REWARD_DIR/openbench-verifier-evidence.json" <<EOF
{
  "schema_version": "openbench-verifier-evidence-v2",
  "openbench_task_content_digest": {
    "scheme": 2,
    "sha256": "__OPENBENCH_TASK_CONTENT_DIGEST__"
  },
  "checker_exit": $RC,
  "parsed_score": $PARSED_SCORE_JSON,
  "reward": $REWARD,
  "verifier_duration_seconds": $DURATION_JSON
}
EOF
rm -f "$OUT_FILE"
exit 0
"""
    return script.replace(
        "__OPENBENCH_TASK_CONTENT_DIGEST__",
        openbench_task_content_digest,
    )


def render_solve_sh() -> str:
    """Oracle: overlay packaged solution files onto the agent workspace cwd."""
    return r"""#!/usr/bin/env bash
# OpenBench → Harbor oracle: copy solution tree onto the workspace (cwd).
set -euo pipefail

SOLUTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Harbor copies solution/ to /solution and runs solve.sh from the workdir.
# Overlay every solution file except this generated runner itself.
while IFS= read -r -d '' src; do
  rel="${src#"$SOLUTION_DIR"/}"
  case "$rel" in
    solve.sh) continue ;;
  esac
  dest="./$rel"
  mkdir -p "$(dirname "$dest")"
  cp -f "$src" "$dest"
done < <(find "$SOLUTION_DIR" -type f -print0)

echo "openbench harbor oracle: solution overlaid"
"""


def _copy_tree_contents(src: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dest, name)
        if os.path.isdir(s) and not os.path.islink(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)


def _write_text(path: str, content: str, *, mode: int | None = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content if content.endswith("\n") else content + "\n")
    if mode is not None:
        os.chmod(path, mode)


def _task_display_name(task_dir: str, tasks_dir: str | None) -> str:
    task_dir = os.path.abspath(task_dir)
    if tasks_dir:
        rel = os.path.relpath(task_dir, os.path.abspath(tasks_dir))
        if not rel.startswith(".."):
            return rel.replace(os.sep, "/")
    return os.path.basename(task_dir)


def export_task(
    task_dir: str,
    out_dir: str,
    *,
    task_name: str | None = None,
    base_image: str = DEFAULT_BASE_IMAGE,
    network_mode: str = "no-network",
) -> dict:
    """Export one OpenBench task directory to a Harbor task directory.

    Returns a small summary dict (paths + workspace provenance).
    Never copies transcripts, results, or auth material.
    """
    task_dir = os.path.abspath(task_dir)
    out_dir = os.path.abspath(out_dir)
    checker = os.path.join(task_dir, "checker.sh")
    instruction = os.path.join(task_dir, "instruction.md")
    if not os.path.isfile(checker):
        raise ExportError(f"not an OpenBench task (missing checker.sh): {task_dir}")
    if not os.path.isfile(instruction):
        raise ExportError(f"missing instruction.md: {task_dir}")

    name = task_name or os.path.basename(task_dir.rstrip(os.sep))
    try:
        content_digest = task_content_digest(
            task_dir,
            scheme=DIGEST_SCHEME_CURRENT,
        )
    except PublishError as exc:
        raise ExportError(f"cannot fingerprint OpenBench task {name}: {exc}") from exc
    if os.path.exists(out_dir):
        if not os.path.isdir(out_dir):
            raise ExportError(f"export path exists and is not a directory: {out_dir}")
        # Replace prior export of this task cleanly.
        for child in os.listdir(out_dir):
            path = os.path.join(out_dir, child)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
    else:
        os.makedirs(out_dir, exist_ok=True)

    with open(instruction, encoding="utf-8") as fh:
        instruction_text = fh.read()
    description = _first_paragraph(instruction_text) or f"OpenBench task {name}"

    # Stage workspace into environment/app/ (materialize git-mode first).
    env_app = os.path.join(out_dir, "environment", "app")
    os.makedirs(env_app, exist_ok=True)
    try:
        provenance = materialize_workspace(task_dir, env_app)
        mode = resolve_workspace_mode(task_dir)
    except WorkspaceError as exc:
        raise ExportError(f"workspace staging failed for {name}: {exc}") from exc

    _write_text(
        os.path.join(out_dir, "environment", "Dockerfile"),
        render_dockerfile(base_image=base_image),
    )
    _write_text(os.path.join(out_dir, "instruction.md"), instruction_text)
    _write_text(
        os.path.join(out_dir, "task.toml"),
        render_task_toml(
            task_name=name,
            description=description,
            workspace_provenance=provenance,
            openbench_task_content_digest=content_digest,
            network_mode=network_mode,
        ),
    )

    # Verifier payload: checker (+ optional checker_data) under tests/.
    tests_dir = os.path.join(out_dir, "tests")
    os.makedirs(tests_dir, exist_ok=True)
    shutil.copy2(checker, os.path.join(tests_dir, "checker.sh"))
    os.chmod(os.path.join(tests_dir, "checker.sh"), 0o755)
    checker_data = os.path.join(task_dir, "checker_data")
    if os.path.isdir(checker_data):
        _copy_tree_contents(checker_data, os.path.join(tests_dir, "checker_data"))
    _write_text(
        os.path.join(tests_dir, "test.sh"),
        render_test_sh(content_digest),
        mode=0o755,
    )

    # Oracle when OpenBench ships a solution/.
    solution_src = os.path.join(task_dir, "solution")
    has_solution = False
    if os.path.isdir(solution_src) and any(
        os.path.isfile(os.path.join(r, f))
        for r, _d, files in os.walk(solution_src)
        for f in files
    ):
        has_solution = True
        solution_dest = os.path.join(out_dir, "solution")
        os.makedirs(solution_dest, exist_ok=True)
        for root, _dirs, files in os.walk(solution_src):
            rel = os.path.relpath(root, solution_src)
            target_root = solution_dest if rel == "." else os.path.join(solution_dest, rel)
            os.makedirs(target_root, exist_ok=True)
            for fname in files:
                shutil.copy2(
                    os.path.join(root, fname),
                    os.path.join(target_root, fname),
                )
        solve_dest = os.path.join(solution_dest, "solve.sh")
        if os.path.isfile(solve_dest):
            # Procedural task oracles are already Harbor-compatible: Harbor
            # invokes solution/solve.sh from the agent workspace. Preserve the
            # task author's procedure instead of replacing it with an overlay.
            os.chmod(solve_dest, os.stat(solve_dest).st_mode | 0o100)
        else:
            _write_text(solve_dest, render_solve_sh(), mode=0o755)

    return {
        "task_name": name,
        "out_dir": out_dir,
        "workspace_mode": mode,
        "workspace_provenance": provenance,
        "openbench_task_content_digest": {
            "scheme": DIGEST_SCHEME_CURRENT,
            "sha256": content_digest,
        },
        "has_solution": has_solution,
    }


def list_exportable_tasks(tasks_dir: str) -> list[tuple[str, str]]:
    """Return ``[(display_name, task_dir), ...]`` under ``tasks_dir``."""
    tasks_dir = os.path.abspath(tasks_dir)
    found = discover_tasks([("export", tasks_dir)])
    return [(display, path) for _tier, display, path in found]


def resolve_export_selection(
    tasks_dir: str,
    task_arg: str,
) -> list[tuple[str, str]]:
    """Resolve ``--task all`` or comma-separated names to concrete task dirs."""
    available = list_exportable_tasks(tasks_dir)
    by_name = {name: path for name, path in available}
    # Also allow basename match for nested imported layouts.
    by_base: dict[str, list[tuple[str, str]]] = {}
    for name, path in available:
        by_base.setdefault(os.path.basename(name), []).append((name, path))

    raw = [t.strip() for t in task_arg.split(",") if t.strip()]
    if not raw:
        raise ExportError("--task must be 'all' or a comma-separated name list")
    if raw == ["all"] or (len(raw) == 1 and raw[0].lower() == "all"):
        if not available:
            raise ExportError(f"no tasks with checker.sh under {tasks_dir}")
        return available

    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token in raw:
        if token in by_name:
            path = by_name[token]
            key = os.path.abspath(path)
            if key not in seen:
                selected.append((token, path))
                seen.add(key)
            continue
        matches = by_base.get(token, [])
        if len(matches) == 1:
            name, path = matches[0]
            key = os.path.abspath(path)
            if key not in seen:
                selected.append((name, path))
                seen.add(key)
            continue
        if len(matches) > 1:
            opts = ", ".join(n for n, _ in matches)
            raise ExportError(
                f"ambiguous task {token!r}; use a full relative path "
                f"(candidates: {opts})"
            )
        raise ExportError(
            f"unknown task {token!r} under {tasks_dir} "
            f"(known: {', '.join(n for n, _ in available) or 'none'})"
        )
    return selected


def export_tasks(
    tasks_dir: str,
    out_root: str,
    task_arg: str,
    *,
    base_image: str = DEFAULT_BASE_IMAGE,
    network_mode: str = "no-network",
) -> list[dict]:
    """Export one or more tasks into ``out_root/<task_name>/``."""
    selected = resolve_export_selection(tasks_dir, task_arg)
    os.makedirs(out_root, exist_ok=True)
    results = []
    for display, task_dir in selected:
        # Flatten nested names (imported tier) into a single directory segment.
        safe_name = display.replace("/", "__")
        dest = os.path.join(os.path.abspath(out_root), safe_name)
        summary = export_task(
            task_dir,
            dest,
            task_name=display.replace(os.sep, "/"),
            base_image=base_image,
            network_mode=network_mode,
        )
        results.append(summary)
    return results


def read_reward_file(reward_dir: str) -> float:
    """Read Harbor ``reward.txt`` (or prefer ``reward.json`` scalar if alone)."""
    json_path = os.path.join(reward_dir, "reward.json")
    txt_path = os.path.join(reward_dir, REWARD_FILENAME)
    if os.path.isfile(json_path):
        import json

        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, (int, float)):
            return float(data)
        if isinstance(data, dict):
            # Prefer a conventional key if present; else first numeric value.
            for key in ("reward", "score", "accuracy"):
                if key in data and isinstance(data[key], (int, float)):
                    return float(data[key])
            for value in data.values():
                if isinstance(value, (int, float)):
                    return float(value)
        raise ExportError(f"unrecognized reward.json in {reward_dir}")
    if not os.path.isfile(txt_path):
        raise ExportError(f"missing reward file under {reward_dir}")
    with open(txt_path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    try:
        return float(raw.splitlines()[-1].strip())
    except ValueError as exc:
        raise ExportError(f"invalid reward.txt contents: {raw!r}") from exc


def run_exported_verifier(
    exported_task_dir: str,
    workspace_dir: str,
    *,
    reward_dir: str | None = None,
) -> float:
    """Run exported ``tests/test.sh`` against ``workspace_dir`` (Harbor-like).

    Sets ``VERIFIER_LOGS_DIR`` so the reward lands in a controllable directory
    without requiring a real ``/logs/verifier`` mount.
    """
    exported_task_dir = os.path.abspath(exported_task_dir)
    workspace_dir = os.path.abspath(workspace_dir)
    test_sh = os.path.join(exported_task_dir, "tests", "test.sh")
    if not os.path.isfile(test_sh):
        raise ExportError(f"exported task missing tests/test.sh: {exported_task_dir}")
    if reward_dir is None:
        reward_dir = os.path.join(workspace_dir, "logs-verifier")
    os.makedirs(reward_dir, exist_ok=True)
    env = dict(os.environ)
    env["VERIFIER_LOGS_DIR"] = reward_dir
    proc = subprocess.run(
        ["bash", test_sh],
        cwd=workspace_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ExportError(
            f"test.sh exited {proc.returncode} (Harbor verifiers should exit 0 "
            f"after writing reward):\n{proc.stdout}"
        )
    return read_reward_file(reward_dir)


def run_exported_oracle(exported_task_dir: str, workspace_dir: str) -> None:
    """Run exported ``solution/solve.sh`` with cwd = agent workspace."""
    solve = os.path.join(exported_task_dir, "solution", "solve.sh")
    if not os.path.isfile(solve):
        raise ExportError(f"exported task has no solution/solve.sh: {exported_task_dir}")
    proc = subprocess.run(
        ["bash", solve],
        cwd=workspace_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ExportError(
            f"solve.sh failed with exit {proc.returncode}:\n{proc.stdout}"
        )


def round_trip_polarity(exported_task_dir: str) -> tuple[float, float]:
    """Simulate Harbor verifier polarity without installing Harbor.

    Returns ``(untouched_reward, after_oracle_reward)``.
    """
    exported_task_dir = os.path.abspath(exported_task_dir)
    staged = os.path.join(exported_task_dir, "environment", "app")
    if not os.path.isdir(staged):
        raise ExportError(
            f"exported task missing environment/app: {exported_task_dir}"
        )

    tmp = tempfile.mkdtemp(prefix="obench_harbor_rt_")
    try:
        untouched = os.path.join(tmp, "untouched")
        solved = os.path.join(tmp, "solved")
        shutil.copytree(staged, untouched)
        shutil.copytree(staged, solved)

        r0 = run_exported_verifier(
            exported_task_dir,
            untouched,
            reward_dir=os.path.join(tmp, "reward_untouched"),
        )
        run_exported_oracle(exported_task_dir, solved)
        r1 = run_exported_verifier(
            exported_task_dir,
            solved,
            reward_dir=os.path.join(tmp, "reward_solved"),
        )
        return r0, r1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="obench export",
        description="Export OpenBench tasks to external harness formats.",
    )
    sub = parser.add_subparsers(dest="target", required=True)

    harbor = sub.add_parser(
        "harbor",
        help="export tasks to Harbor format (one-way bridge)",
    )
    harbor.add_argument(
        "--task",
        required=True,
        help="comma-separated task names, or 'all'",
    )
    harbor.add_argument(
        "--tasks-dir",
        default=None,
        help="OpenBench tasks root (default: discovery / config)",
    )
    harbor.add_argument(
        "--out",
        required=True,
        help="output directory for Harbor task directories",
    )
    harbor.add_argument(
        "--base-image",
        default=DEFAULT_BASE_IMAGE,
        help=f"Docker base image for environment/Dockerfile (default: {DEFAULT_BASE_IMAGE})",
    )
    harbor.add_argument(
        "--network-mode",
        default="no-network",
        choices=("no-network", "public", "allowlist"),
        help="Harbor [environment].network_mode (default: no-network)",
    )

    args = parser.parse_args(argv)
    if args.target != "harbor":
        parser.error(f"unknown export target {args.target!r}")

    try:
        tasks_dir = resolve_tasks_dir(args.tasks_dir)
    except TasksDirError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not os.path.isdir(tasks_dir):
        print(f"error: tasks dir does not exist: {tasks_dir}", file=sys.stderr)
        return 2

    try:
        results = export_tasks(
            tasks_dir,
            args.out,
            args.task,
            base_image=args.base_image,
            network_mode=args.network_mode,
        )
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"exported {len(results)} Harbor task(s) → {os.path.abspath(args.out)}")
    for summary in results:
        prov = summary.get("workspace_provenance") or {}
        sha = prov.get("resolved_sha")
        extra = f" sha={sha[:12]}" if sha else ""
        sol = " +oracle" if summary.get("has_solution") else ""
        print(
            f"  {summary['task_name']}: {summary['out_dir']} "
            f"({summary['workspace_mode']}{extra}{sol})"
        )
    print(
        "Next: harbor run -p <task-dir> -a <agent> -m <model>  "
        "(see docs/harbor-export.md)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
