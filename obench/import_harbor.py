#!/usr/bin/env python3
"""Harbor → OpenBench task importer (reverse of ``export_harbor``).

Pulls Harbor-format task directories (``instruction.md`` + ``task.toml`` +
``environment/`` + ``tests/test.sh`` + optional ``solution/solve.sh``) into
OpenBench's files-plus-checker layout so they can run under OpenBench harness
adapters, metering, and reporting.

Never runs Docker and never grants network access to imported scripts.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob as _glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from . import __version__
from .export_harbor import ExportError, read_reward_file
from .validate_tasks import effective_score, parse_score, run_checker

IMPORTER_NAME = "obench import harbor"
HARBOR_SCHEMA_HINT = "1.3"

# Instruction text that leaks Harbor grading internals is rejected.
_INSTRUCTION_BANNED = re.compile(
    r"/logs/verifier|reward\.(txt|json)",
    re.IGNORECASE,
)

# solve.sh content that likely needs container/network — skip auto materialize.
# Avoid bare words like "network" (comments) or "pip" inside "pipefail".
_SOLVE_UNSAFE = re.compile(
    r"(?i)(\bcurl\b|\bwget\b|\bdocker\b|\bpodman\b|\bapt-get\b|\bapk\s+add\b"
    r"|\bpip3?\s+install\b|\bnpm\s+install\b|\byarn\s+add\b"
    r"|\bssh\b|\bnmap\b|https?://)"
)

_COPY_RE = re.compile(
    r"^\s*(COPY|ADD)\s+"
    r"(?:--from=\S+\s+)?"
    r"(?:--chown=\S+\s+)?"
    r"(?:--chmod=\S+\s+)?"
    r"(.+)$",
    re.IGNORECASE,
)
_FROM_RE = re.compile(r"^\s*FROM\s+(\S+)", re.IGNORECASE)
_WORKDIR_RE = re.compile(r"^\s*WORKDIR\s+(\S+)", re.IGNORECASE)
_RUN_RE = re.compile(r"^\s*RUN\b", re.IGNORECASE)
_STAGE_AS_RE = re.compile(r"\s+AS\s+(\S+)\s*$", re.IGNORECASE)
# Harbor agent workdir paths in solve.sh / tests (rewrite for local materialize).
_HARBOR_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/app(?=/|[\"']|$|\s)"
)


class HarborImportError(ValueError):
    """User-facing import failure for one task or the whole run."""


@dataclass
class DockerfileAnalysis:
    """Parsed Harbor ``environment/Dockerfile`` staging plan."""

    base_image: str | None = None
    workdir: str = "/app"
    copy_ops: list[tuple[str, str]] = field(default_factory=list)  # (src, dest)
    docker_required: bool = False
    reasons: list[str] = field(default_factory=list)
    packages_hint: list[str] = field(default_factory=list)
    multi_stage: bool = False


@dataclass
class ImportResult:
    """Per-task import summary for the CLI report."""

    task_name: str
    out_dir: str
    ok: bool = True
    docker_required: bool = False
    solution_materialized: bool = False
    needs_manual_attention: list[str] = field(default_factory=list)
    schema_version: str | None = None
    source_path: str = ""
    error: str | None = None
    validated: bool | None = None  # polarity when solution present
    notes: list[str] = field(default_factory=list)


def _write_text(path: str, content: str, *, mode: int | None = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content if content.endswith("\n") else content + "\n")
    if mode is not None:
        os.chmod(path, mode)


def _copy_tree_contents(src: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dest, name)
        if os.path.isdir(s) and not os.path.islink(s):
            if os.path.exists(d):
                shutil.rmtree(d)
            shutil.copytree(s, d)
        else:
            os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
            shutil.copy2(s, d)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parse_schema_version(task_toml_text: str) -> str | None:
    """Extract Harbor schema_version or legacy version from task.toml."""
    m = re.search(
        r'^\s*schema_version\s*=\s*["\']([^"\']+)["\']',
        task_toml_text,
        re.MULTILINE,
    )
    if m:
        return m.group(1)
    m = re.search(
        r'^\s*version\s*=\s*["\']([^"\']+)["\']',
        task_toml_text,
        re.MULTILINE,
    )
    return m.group(1) if m else None


def check_instruction_safe(instruction_text: str) -> None:
    """Fail if the agent-facing instruction leaks Harbor grading internals."""
    if _INSTRUCTION_BANNED.search(instruction_text):
        raise HarborImportError(
            "instruction.md references Harbor grading internals "
            "(/logs/verifier or reward.txt/reward.json). Refusing to import: "
            "agent-facing prompts must not describe the verifier reward path. "
            "Edit the upstream instruction (or strip those lines) and retry."
        )


def map_reward_to_checker(reward: float) -> tuple[int, str | None]:
    """Map Harbor reward float to OpenBench (exit_code, optional SCORE line).

    Mirrors the inverse of ``export_harbor.map_checker_to_reward``:
    reward >= 1.0 → exit 0; 0 < reward < 1 → SCORE + exit 1; else exit 1.
    """
    if reward >= 1.0:
        return 0, None
    if reward > 0.0:
        return 1, f"SCORE: {reward}"
    return 1, None


def _iter_dockerfile_logical_lines(dockerfile_text: str):
    """Yield logical Dockerfile lines with backslash continuations joined."""
    buf = ""
    for raw in dockerfile_text.splitlines():
        if buf:
            cont = raw.strip()
            if cont.endswith("\\"):
                buf += cont[:-1].rstrip() + " "
            else:
                buf += cont
                yield buf
                buf = ""
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf = line[:-1].rstrip() + " "
        else:
            yield line
    if buf:
        yield buf


def _resolve_container_path(path: str, workdir: str) -> str:
    """Resolve a container path (absolute or relative) against ``workdir``.

    Preserves a trailing slash when ``path`` denotes a directory destination
    (``./``, ``foo/``, absolute ``/app/``).
    """
    path = (path or "").strip()
    workdir = (workdir or "/").strip() or "/"
    if not path or path in (".", "./"):
        base = workdir.rstrip("/") or ""
        return (base + "/") if base else "/"
    trailing = path.endswith("/") and path != "/"
    if path.startswith("/"):
        resolved = os.path.normpath(path).replace("\\", "/")
    else:
        base = workdir.rstrip("/") or ""
        joined = f"{base}/{path}" if base else f"/{path}"
        resolved = os.path.normpath(joined).replace("\\", "/")
        if not resolved.startswith("/"):
            resolved = "/" + resolved
    if resolved != "/" and trailing:
        return resolved + "/"
    return resolved if resolved != "/" else "/"


def _workspace_rel_for_copy_dest(
    dest: str, workdir: str
) -> tuple[str, bool, bool]:
    """Map absolute container COPY dest → (workspace_rel, unusual, dest_is_dir).

    Tries the final Dockerfile WORKDIR first, then Harbor's common ``/app`` root
    (COPY often targets ``/app`` before a later WORKDIR change).
    """
    dest_is_dir = dest.endswith("/") and dest != "/"
    dest_norm = dest.rstrip("/") if dest != "/" else "/"
    roots: list[str] = []
    for root in (workdir.rstrip("/") or "/", "/app"):
        if root not in roots:
            roots.append(root)
    for root in roots:
        if dest_norm == root:
            return "", False, True
        if dest_norm.startswith(root + "/"):
            return dest_norm[len(root) + 1 :], False, dest_is_dir
    rel = os.path.basename(dest_norm)
    if not rel or rel in (".", ".."):
        rel = "staged"
    return rel, True, dest_is_dir


def parse_dockerfile(dockerfile_text: str) -> DockerfileAnalysis:
    """Parse a Harbor environment Dockerfile for workspace staging + complexity.

    Staging-only images (FROM + WORKDIR + COPY/ADD of local files) import
    cleanly. RUN installs, multi-stage builds, remote ADD, or compose-adjacent
    markers classify the task as DOCKER-REQUIRED (still imported).

    Relative COPY/ADD destinations are resolved against the WORKDIR in effect
    at that instruction (not the final WORKDIR).
    """
    analysis = DockerfileAnalysis()
    from_count = 0
    current_workdir = "/app"  # Harbor / TB convention when WORKDIR omitted early
    for line in _iter_dockerfile_logical_lines(dockerfile_text):
        m_from = _FROM_RE.match(line)
        if m_from:
            from_count += 1
            image = m_from.group(1)
            # Scratch/builder stages after the first mark multi-stage.
            if from_count == 1:
                analysis.base_image = image.split(" AS ")[0].split(" as ")[0]
                # Docker default WORKDIR is "/"; Harbor tasks nearly always
                # set WORKDIR /app — keep /app as staging default until seen.
                current_workdir = "/app"
                analysis.workdir = "/app"
            else:
                analysis.multi_stage = True
                analysis.docker_required = True
                analysis.reasons.append("multi-stage Dockerfile (multiple FROM)")
            if _STAGE_AS_RE.search(line) and from_count > 1:
                pass
            continue

        m_wd = _WORKDIR_RE.match(line)
        if m_wd:
            current_workdir = _resolve_container_path(
                m_wd.group(1), current_workdir
            ).rstrip("/") or "/"
            analysis.workdir = current_workdir
            continue

        if _RUN_RE.match(line):
            analysis.docker_required = True
            analysis.reasons.append(f"RUN directive: {line[:80]}")
            # Rough package hints for REQUIREMENTS.md (stop at shell separators).
            for pkg in re.findall(
                r"(?:apt-get\s+install\s+(?:-y\s+)?|apk\s+add\s+(?:--no-cache\s+)?)"
                r"([^;&\n]+)",
                line,
                re.IGNORECASE,
            ):
                for token in pkg.split():
                    if token.startswith("-") or token in ("&&", "||"):
                        continue
                    if token.startswith("/") or token in ("rm", "rmdir"):
                        continue
                    analysis.packages_hint.append(token)
            if re.search(r"\bpip3?\s+install\b", line, re.IGNORECASE):
                analysis.packages_hint.append("(pip packages — see Dockerfile RUN)")
            continue

        m_copy = _COPY_RE.match(line)
        if m_copy:
            kind = m_copy.group(1).upper()
            rest = m_copy.group(2).strip()
            if re.search(r"--from=", line, re.IGNORECASE):
                analysis.docker_required = True
                analysis.reasons.append(f"{kind} --from= (multi-stage artifact)")
                continue
            # Shell-style tokens; last is dest, earlier are sources.
            # Ignore JSON-form COPY for simplicity → mark docker-required.
            if rest.startswith("["):
                analysis.docker_required = True
                analysis.reasons.append(f"JSON-form {kind} not auto-staged")
                continue
            parts = rest.split()
            if len(parts) < 2:
                analysis.docker_required = True
                analysis.reasons.append(f"unparseable {kind}: {line[:80]}")
                continue
            dest_raw = parts[-1]
            # Resolve relative dests (., ./, foo) against WORKDIR at this line.
            dest = _resolve_container_path(dest_raw, current_workdir)
            sources = parts[:-1]
            for src in sources:
                if src.startswith(("http://", "https://", "git@", "ftp://")):
                    analysis.docker_required = True
                    analysis.reasons.append(f"remote {kind} source: {src}")
                    continue
                analysis.copy_ops.append((src, dest))
            continue

        # Other directives (ENV, USER, EXPOSE, …) are usually fine; compose
        # hints and privileged bits escalate.
        if re.match(r"^\s*(ENTRYPOINT|CMD|VOLUME)\b", line, re.IGNORECASE):
            analysis.docker_required = True
            analysis.reasons.append(f"{line.split()[0]} directive present")
    if from_count == 0:
        analysis.docker_required = True
        analysis.reasons.append("Dockerfile missing FROM")
    return analysis


def stage_workspace_from_dockerfile(
    harbor_task_dir: str,
    workspace_dest: str,
    analysis: DockerfileAnalysis | None = None,
) -> DockerfileAnalysis:
    """Copy Dockerfile-staged sources into ``workspace_dest``.

    Build context is Harbor's ``environment/`` directory (Harbor convention).
    """
    env_dir = os.path.join(harbor_task_dir, "environment")
    df_path = os.path.join(env_dir, "Dockerfile")
    if not os.path.isfile(df_path):
        raise HarborImportError(f"missing environment/Dockerfile: {harbor_task_dir}")
    analysis = analysis or parse_dockerfile(_read_text(df_path))
    os.makedirs(workspace_dest, exist_ok=True)
    env_abs = os.path.abspath(env_dir)

    workdir = analysis.workdir.rstrip("/") or "/"
    staged_any = False
    for src_rel, dest in analysis.copy_ops:
        src_path = os.path.normpath(os.path.join(env_dir, src_rel))
        if not os.path.abspath(src_path).startswith(env_abs + os.sep) and (
            os.path.abspath(src_path) != env_abs
        ):
            analysis.docker_required = True
            analysis.reasons.append(f"COPY path escapes environment/: {src_rel}")
            continue
        rel, unusual, dest_is_dir = _workspace_rel_for_copy_dest(dest, workdir)
        if unusual:
            analysis.docker_required = True
            analysis.reasons.append(
                f"COPY dest {dest!r} outside WORKDIR {workdir!r}; "
                f"staged as workspace/{rel}"
            )

        target = workspace_dest if not rel else os.path.join(workspace_dest, rel)
        if not os.path.exists(src_path):
            analysis.docker_required = True
            analysis.reasons.append(f"COPY source missing: {src_rel}")
            continue
        if os.path.isdir(src_path):
            # COPY dir/ dest/ → contents when dest is a directory path.
            dest_norm = dest.rstrip("/") if dest != "/" else "/"
            if dest_is_dir or dest.endswith("/") or not rel or dest_norm in (
                workdir, "/app",
            ):
                _copy_tree_contents(src_path, target)
            else:
                if os.path.exists(target):
                    shutil.rmtree(target)
                shutil.copytree(src_path, target)
            staged_any = True
        else:
            # File → directory dest keeps basename (Docker COPY semantics).
            if not rel or dest_is_dir or dest.endswith("/"):
                os.makedirs(target if rel else workspace_dest, exist_ok=True)
                dest_file = os.path.join(
                    target if rel else workspace_dest,
                    os.path.basename(src_path),
                )
                shutil.copy2(src_path, dest_file)
            else:
                os.makedirs(os.path.dirname(target) or workspace_dest, exist_ok=True)
                shutil.copy2(src_path, target)
            staged_any = True

    if not staged_any:
        # Fallback: if environment/app exists (OpenBench export shape), use it.
        app = os.path.join(env_dir, "app")
        if os.path.isdir(app):
            _copy_tree_contents(app, workspace_dest)
            staged_any = True
            analysis.reasons.append(
                "staged workspace from environment/app/ (COPY sources empty/missing)"
            )
        else:
            # Empty agent workspace is common for TB tasks that create files
            # from scratch — note it, but do not escalate to DOCKER-REQUIRED
            # unless other Dockerfile complexity already did.
            analysis.reasons.append(
                "no local COPY/ADD sources staged into workspace "
                "(empty agent workspace — agent creates files from scratch)"
            )
    return analysis


def reward_from_dir(reward_dir: str) -> float:
    """Read Harbor reward.txt / reward.json (same contract as exporter)."""
    return read_reward_file(reward_dir)


def render_checker_sh() -> str:
    """OpenBench checker that runs Harbor ``tests/test.sh`` and maps reward."""
    return r"""#!/usr/bin/env bash
# Generated by obench import harbor — Harbor tests/ → OpenBench exit/SCORE.
# cwd = agent workspace; TASK_DIR = OpenBench task directory.
set -uo pipefail

HARBOR_TESTS="${TASK_DIR}/checker_data/harbor-tests"
if [ ! -f "${HARBOR_TESTS}/test.sh" ]; then
  echo "obench harbor import: missing checker_data/harbor-tests/test.sh" >&2
  exit 1
fi

REWARD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/obench-harbor-reward.XXXXXX")"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/obench-harbor-tests.XXXXXX")"
cleanup() { rm -rf "${REWARD_DIR}" "${RUN_DIR}"; }
trap cleanup EXIT

# Stage a writable copy of Harbor tests and rewrite absolute Harbor paths so
# local --exec local can run without /logs or /tests mounts.
cp -a "${HARBOR_TESTS}/." "${RUN_DIR}/"
export VERIFIER_LOGS_DIR="${REWARD_DIR}"
# Prefer python rewrite for reliable path substitution across sh/py files.
python3 - "${RUN_DIR}" "${REWARD_DIR}" <<'PY'
import os, re, sys
run_dir, reward_dir = sys.argv[1], sys.argv[2]
cwd = os.getcwd()
skip_ext = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
            ".gz", ".bz2", ".xz", ".7z", ".whl", ".so", ".dylib", ".o", ".a",
            ".pyc", ".pyo", ".class", ".jar", ".bin", ".dat", ".sqlite"}
for root, _dirs, files in os.walk(run_dir):
    for name in files:
        path = os.path.join(root, name)
        ext = os.path.splitext(name)[1].lower()
        if ext in skip_ext:
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        new = text.replace("/logs/verifier", reward_dir)
        new = re.sub(r"(?<![A-Za-z0-9_])/tests(?![A-Za-z0-9_])", run_dir, new)
        # Harbor agent workdir is often /app; OpenBench checker cwd is workspace.
        new = re.sub(r"(?<![A-Za-z0-9_])/app(?=/|[\"']|$|\s)", cwd, new)
        if new != text:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
PY

set +e
bash "${RUN_DIR}/test.sh" >"${REWARD_DIR}/checker-stdout.txt" 2>&1
TEST_RC=$?
set -e
cat "${REWARD_DIR}/checker-stdout.txt" 2>/dev/null || true

python3 - "${REWARD_DIR}" "${TEST_RC}" <<'PY'
import json, os, sys

reward_dir = sys.argv[1]
test_rc = int(sys.argv[2])

def read_reward(d):
    jp = os.path.join(d, "reward.json")
    tp = os.path.join(d, "reward.txt")
    if os.path.isfile(jp):
        with open(jp, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, (int, float)):
            return float(data)
        if isinstance(data, dict):
            for key in ("reward", "score", "accuracy"):
                if key in data and isinstance(data[key], (int, float)):
                    return float(data[key])
            for value in data.values():
                if isinstance(value, (int, float)):
                    return float(value)
        raise SystemExit("unrecognized reward.json")
    if os.path.isfile(tp):
        with open(tp, encoding="utf-8") as fh:
            raw = fh.read().strip()
        if not raw:
            raise SystemExit("empty reward.txt")
        return float(raw.splitlines()[-1].strip())
    raise SystemExit("missing reward.txt/reward.json (Harbor verifier contract)")

try:
    reward = read_reward(reward_dir)
except SystemExit as exc:
    print(f"obench harbor import: {exc}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"obench harbor import: failed to read reward: {exc}", file=sys.stderr)
    sys.exit(1)

# Harbor test.sh should exit 0 after writing reward; still honor the file.
if reward >= 1.0:
    sys.exit(0)
if reward > 0.0:
    print(f"SCORE: {reward}")
    sys.exit(1)
sys.exit(1)
PY
"""


def _solution_file_entries(solution_dir: str) -> list[str]:
    """Relative paths of non-script solution files (oracle payloads)."""
    entries = []
    for root, _dirs, files in os.walk(solution_dir):
        for name in files:
            if name.endswith(".sh"):
                continue
            rel = os.path.relpath(os.path.join(root, name), solution_dir)
            entries.append(rel)
    return sorted(entries)


def solve_sh_is_safe_to_run(solve_text: str) -> tuple[bool, str]:
    """Conservative gate: refuse scripts that look like they need network/docker."""
    if _SOLVE_UNSAFE.search(solve_text):
        return False, "solve.sh references network/package/container tooling"
    # Heredoc-only file writers and cp/mv/sed/echo are OK.
    return True, ""


def _rewrite_harbor_paths_for_local(
    text: str,
    local_root: str,
    *,
    harbor_workdir: str = "/app",
) -> str:
    """Rewrite Harbor absolute workdir paths onto a local workspace root.

    Terminal-Bench solve scripts commonly ``cat > /app/foo``; locally we point
    those at the materialization sandbox workspace instead.
    """
    local_root = os.path.abspath(local_root)
    # Longer prefixes first so /app/personal-site wins over /app.
    prefixes: list[str] = []
    for prefix in (harbor_workdir.rstrip("/") or "/app", "/app"):
        if prefix not in prefixes:
            prefixes.append(prefix)
    prefixes.sort(key=len, reverse=True)

    out = text
    for prefix in prefixes:
        if prefix == "/app":
            out = _HARBOR_ABS_PATH_RE.sub(lambda _m: local_root, out)
            continue
        # Escape for regex; require path boundary after the prefix.
        pat = re.compile(
            r"(?<![A-Za-z0-9_])"
            + re.escape(prefix)
            + r"(?=/|[\"']|$|\s)"
        )
        out = pat.sub(lambda _m: local_root, out)
    return out


def materialize_solution_from_harbor(
    harbor_task_dir: str,
    workspace_dir: str,
    solution_dest: str,
    *,
    timeout_sec: float = 60.0,
) -> tuple[bool, list[str]]:
    """Build OpenBench ``solution/`` from Harbor ``solution/``.

    Returns ``(materialized, attention_notes)``.

    Strategy (conservative):
    1. If Harbor ships non-``.sh`` files beside ``solve.sh``, copy them
       (covers OpenBench→Harbor→OpenBench round-trips).
    2. Else, when ``solve.sh`` looks like a deterministic local writer, run it
       under ``env -i`` on a sandbox copy of the workspace and collect the diff.
    3. Otherwise skip and ask for manual materialization.
    """
    notes: list[str] = []
    sol_src = os.path.join(harbor_task_dir, "solution")
    if not os.path.isdir(sol_src):
        return False, ["no Harbor solution/ directory"]
    solve = os.path.join(sol_src, "solve.sh")
    payloads = _solution_file_entries(sol_src)
    if payloads:
        os.makedirs(solution_dest, exist_ok=True)
        for rel in payloads:
            src = os.path.join(sol_src, rel)
            dest = os.path.join(solution_dest, rel)
            os.makedirs(os.path.dirname(dest) or solution_dest, exist_ok=True)
            shutil.copy2(src, dest)
        notes.append(
            f"copied {len(payloads)} non-script solution file(s) from Harbor solution/"
        )
        return True, notes

    if not os.path.isfile(solve):
        return False, ["Harbor solution/ has no solve.sh and no payload files"]

    solve_text = _read_text(solve)
    ok, reason = solve_sh_is_safe_to_run(solve_text)
    if not ok:
        return False, [
            f"skipped solve.sh materialization: {reason}",
            "needs manual solution/ materialization (or re-run under Docker offline)",
        ]

    sandbox = tempfile.mkdtemp(prefix="obench_harbor_solve_")
    try:
        # Snapshot before/after relative to a copy of the workspace.
        work = os.path.join(sandbox, "work")
        shutil.copytree(workspace_dir, work)
        before = _file_fingerprint_tree(work)
        # Also expose Harbor solution dir like Harbor does (/solution).
        sol_mount = os.path.join(sandbox, "solution")
        shutil.copytree(sol_src, sol_mount)
        # TB/Harbor solve.sh often writes absolute `/app/...` paths. Rewrite
        # those (and the Dockerfile WORKDIR if different) onto the sandbox
        # workspace so materialization works without Docker.
        harbor_workdir = "/app"
        df_path = os.path.join(harbor_task_dir, "environment", "Dockerfile")
        if os.path.isfile(df_path):
            harbor_workdir = (
                parse_dockerfile(_read_text(df_path)).workdir or "/app"
            )
        solve_run = os.path.join(sandbox, "solve-run.sh")
        rewritten = _rewrite_harbor_paths_for_local(
            solve_text, work, harbor_workdir=harbor_workdir
        )
        _write_text(solve_run, rewritten, mode=0o755)
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": sandbox,
            "TMPDIR": sandbox,
            "LANG": "C.UTF-8",
        }
        proc = subprocess.run(
            ["env", "-i", *[f"{k}={v}" for k, v in env.items()], "bash", solve_run],
            cwd=work,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.returncode != 0:
            return False, [
                f"solve.sh exited {proc.returncode}; skipped materialization",
                (proc.stdout or "")[:500],
            ]
        after = _file_fingerprint_tree(work)
        changed = sorted(set(after) - set(before))
        # Also include files whose content changed.
        for rel in set(before) & set(after):
            if before[rel] != after[rel]:
                changed.append(rel)
        changed = sorted(set(changed))
        if not changed:
            return False, [
                "solve.sh ran but produced no workspace file changes",
                "needs manual solution/ materialization",
            ]
        os.makedirs(solution_dest, exist_ok=True)
        for rel in changed:
            src = os.path.join(work, rel)
            if not os.path.isfile(src):
                continue
            dest = os.path.join(solution_dest, rel)
            os.makedirs(os.path.dirname(dest) or solution_dest, exist_ok=True)
            shutil.copy2(src, dest)
        notes.append(
            f"materialized {len(changed)} file(s) by running solve.sh under env -i "
            f"(no network)"
        )
        return True, notes
    except subprocess.TimeoutExpired:
        return False, [
            f"solve.sh timed out after {timeout_sec}s",
            "needs manual solution/ materialization",
        ]
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _file_fingerprint_tree(root: str) -> dict[str, str]:
    """Map relative file path → sha256 hex (content fingerprint)."""
    import hashlib

    out: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            out[rel.replace(os.sep, "/")] = h.hexdigest()
    return out


def render_provenance(
    *,
    source_path: str,
    schema_version: str | None,
    docker_required: bool,
    docker_reasons: list[str],
    solution_notes: list[str],
    attention: list[str],
    collection: str | None,
    import_date: str | None = None,
) -> str:
    """Render per-task PROVENANCE.md."""
    date = import_date or _dt.date.today().isoformat()
    lines = [
        "# Provenance",
        "",
        f"- **Imported by**: {IMPORTER_NAME} (obench {__version__})",
        f"- **Import date**: {date}",
        f"- **Source path**: `{source_path}`",
        f"- **Harbor schema_version seen**: {schema_version or 'unknown'}",
    ]
    if collection:
        lines.append(f"- **OpenBench collection**: `{collection}`")
    lines += [
        "",
        "## License",
        "",
        "**You must verify the upstream dataset / task license before publishing",
        "or redistributing this imported task.** The importer does not inspect or",
        "grant rights. Harbor itself is Apache-2.0; individual Harbor registry",
        "datasets and Terminal-Bench tasks carry their own terms.",
        "",
        "## Auto-converted",
        "",
        "- `instruction.md` copied from Harbor (agent-facing text unchanged).",
        "- `workspace/` staged from `environment/Dockerfile` COPY/ADD sources",
        "  (build context = `environment/`).",
        "- `checker_data/harbor-tests/` holds Harbor `tests/` (verifier-owned;",
        "  not visible as agent workspace).",
        "- `checker.sh` wraps Harbor `tests/test.sh`, maps `reward.txt` /",
        "  `reward.json` → OpenBench exit 0 / `SCORE:` / fail",
        "  (same field preference as `obench export harbor`: JSON keys",
        "  `reward`, `score`, `accuracy`, else first numeric).",
    ]
    if solution_notes:
        lines.append("- Solution handling:")
        for n in solution_notes:
            lines.append(f"  - {n}")
    else:
        lines.append("- `solution/`: not created (see Needs attention).")

    lines += ["", "## Needs attention", ""]
    if docker_required:
        lines.append("- **DOCKER-REQUIRED**: local `--exec local` may fail.")
        for r in docker_reasons:
            lines.append(f"  - {r}")
        lines.append(
            "  - Prefer `--exec docker` with an image that matches the Harbor "
            "base image / packages noted in `REQUIREMENTS.md`."
        )
    if attention:
        for a in attention:
            lines.append(f"- {a}")
    if not docker_required and not attention:
        lines.append("- None noted by the importer.")
    lines.append("")
    return "\n".join(lines)


def render_requirements_md(analysis: DockerfileAnalysis) -> str:
    """Guidance for DOCKER-REQUIRED (and optional) imported tasks."""
    lines = [
        "# Requirements (Harbor import)",
        "",
        "This task was imported from a Harbor environment that may need more",
        "than a bare workspace checkout.",
        "",
    ]
    if analysis.base_image:
        lines.append(f"- **Harbor base image**: `{analysis.base_image}`")
        lines.append(
            "- **OpenBench Docker lane**: map packages from that image into "
            "`openbench-harness:latest` (or a custom image) when using "
            "`--exec docker`."
        )
    if analysis.packages_hint:
        lines.append("- **Packages hinted from Dockerfile RUN**:")
        for p in analysis.packages_hint:
            lines.append(f"  - `{p}`")
    if analysis.reasons:
        lines.append("- **Why DOCKER-REQUIRED**:")
        for r in analysis.reasons:
            lines.append(f"  - {r}")
    lines += [
        "",
        "The importer does **not** run Docker. Validate with",
        "`obench validate --tasks-dir …` after extending the image if needed.",
        "",
    ]
    return "\n".join(lines)


def is_harbor_task_dir(path: str) -> bool:
    """True if ``path`` looks like a Harbor task directory."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return False
    if not os.path.isfile(os.path.join(path, "instruction.md")):
        return False
    if not os.path.isfile(os.path.join(path, "tests", "test.sh")):
        return False
    # Dockerfile or task.toml — either signals Harbor layout.
    env_df = os.path.join(path, "environment", "Dockerfile")
    return os.path.isfile(env_df) or os.path.isfile(os.path.join(path, "task.toml"))


def discover_harbor_tasks(from_spec: str) -> list[str]:
    """Resolve ``--from`` dir / glob to Harbor task directories."""
    matches: list[str] = []
    # Expand globs; also accept a plain directory.
    expanded = sorted(_glob.glob(from_spec)) if any(
        ch in from_spec for ch in "*?[]"
    ) else [from_spec]
    if not expanded and any(ch in from_spec for ch in "*?[]"):
        raise HarborImportError(f"no matches for --from {from_spec!r}")

    candidates: list[str] = []
    for item in expanded:
        item = os.path.abspath(item)
        if not os.path.exists(item):
            raise HarborImportError(f"--from path does not exist: {item}")
        if is_harbor_task_dir(item):
            candidates.append(item)
            continue
        if os.path.isdir(item):
            # Immediate children first, then one-level nesting (dataset packs).
            for name in sorted(os.listdir(item)):
                child = os.path.join(item, name)
                if is_harbor_task_dir(child):
                    candidates.append(child)
            if not any(is_harbor_task_dir(os.path.join(item, n)) for n in os.listdir(item)):
                for dirpath, dirnames, _files in os.walk(item):
                    if is_harbor_task_dir(dirpath):
                        candidates.append(dirpath)
                        dirnames[:] = []
        else:
            raise HarborImportError(f"--from is not a Harbor task or directory: {item}")

    # De-dupe preserving order.
    seen: set[str] = set()
    for c in candidates:
        key = os.path.abspath(c)
        if key not in seen:
            seen.add(key)
            matches.append(key)
    if not matches:
        raise HarborImportError(
            f"no Harbor tasks found under {from_spec!r} "
            "(need instruction.md + tests/test.sh + environment/Dockerfile "
            "or task.toml)"
        )
    return matches


def import_task(
    harbor_task_dir: str,
    out_dir: str,
    *,
    collection: str | None = None,
) -> ImportResult:
    """Import one Harbor task directory into an OpenBench task directory."""
    harbor_task_dir = os.path.abspath(harbor_task_dir)
    out_dir = os.path.abspath(out_dir)
    name = os.path.basename(harbor_task_dir.rstrip(os.sep))
    result = ImportResult(
        task_name=name,
        out_dir=out_dir,
        source_path=harbor_task_dir,
    )

    try:
        if not is_harbor_task_dir(harbor_task_dir):
            raise HarborImportError(f"not a Harbor task directory: {harbor_task_dir}")

        instruction_path = os.path.join(harbor_task_dir, "instruction.md")
        instruction_text = _read_text(instruction_path)
        check_instruction_safe(instruction_text)

        schema = None
        toml_path = os.path.join(harbor_task_dir, "task.toml")
        if os.path.isfile(toml_path):
            schema = parse_schema_version(_read_text(toml_path))
        result.schema_version = schema

        if os.path.exists(out_dir):
            if not os.path.isdir(out_dir):
                raise HarborImportError(f"out path exists and is not a directory: {out_dir}")
            for child in os.listdir(out_dir):
                path = os.path.join(out_dir, child)
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
        else:
            os.makedirs(out_dir, exist_ok=True)

        _write_text(os.path.join(out_dir, "instruction.md"), instruction_text)

        workspace = os.path.join(out_dir, "workspace")
        analysis = stage_workspace_from_dockerfile(harbor_task_dir, workspace)
        result.docker_required = analysis.docker_required
        if analysis.docker_required:
            result.needs_manual_attention.append("DOCKER-REQUIRED")
            result.notes.extend(analysis.reasons)

        # Harbor tests → checker-owned data (not agent workspace).
        tests_src = os.path.join(harbor_task_dir, "tests")
        tests_dest = os.path.join(out_dir, "checker_data", "harbor-tests")
        if os.path.isdir(tests_src):
            _copy_tree_contents(tests_src, tests_dest)
        else:
            raise HarborImportError(f"missing tests/: {harbor_task_dir}")
        _write_text(
            os.path.join(out_dir, "checker.sh"),
            render_checker_sh(),
            mode=0o755,
        )

        solution_dest = os.path.join(out_dir, "solution")
        materialized, sol_notes = materialize_solution_from_harbor(
            harbor_task_dir, workspace, solution_dest
        )
        result.solution_materialized = materialized
        result.notes.extend(sol_notes)
        if not materialized:
            result.needs_manual_attention.append("needs-manual-solution")
            # Remove empty solution/ if we created nothing.
            if os.path.isdir(solution_dest) and not any(
                os.path.isfile(os.path.join(r, f))
                for r, _d, files in os.walk(solution_dest)
                for f in files
            ):
                shutil.rmtree(solution_dest, ignore_errors=True)

        attention = [
            a for a in result.needs_manual_attention if a != "DOCKER-REQUIRED"
        ]
        _write_text(
            os.path.join(out_dir, "PROVENANCE.md"),
            render_provenance(
                source_path=harbor_task_dir,
                schema_version=schema,
                docker_required=analysis.docker_required,
                docker_reasons=list(analysis.reasons),
                solution_notes=sol_notes,
                attention=attention,
                collection=collection,
            ),
        )
        if analysis.docker_required:
            _write_text(
                os.path.join(out_dir, "REQUIREMENTS.md"),
                render_requirements_md(analysis),
            )

        # Lightweight marker for tooling.
        marker = {
            "importer": IMPORTER_NAME,
            "obench_version": __version__,
            "source_path": harbor_task_dir,
            "schema_version": schema,
            "docker_required": analysis.docker_required,
            "base_image": analysis.base_image,
            "solution_materialized": materialized,
        }
        _write_text(
            os.path.join(out_dir, "harbor-import.json"),
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
        )
        return result
    except HarborImportError as exc:
        result.ok = False
        result.error = str(exc)
        return result
    except ExportError as exc:
        result.ok = False
        result.error = str(exc)
        return result


def validate_imported_task(task_dir: str) -> tuple[bool, str]:
    """Run polarity checks when ``solution/`` exists. Returns (ok, detail)."""
    if not os.path.isdir(os.path.join(task_dir, "solution")):
        return False, "no solution/ (skipped polarity)"
    ws_code, ws_out, ws_raw = run_checker(task_dir, overlay_solution_flag=False)
    sol_code, sol_out, sol_raw = run_checker(task_dir, overlay_solution_flag=True)
    ws_score = effective_score(ws_code, ws_raw if ws_raw is not None else parse_score(ws_out or ""))
    sol_score = effective_score(sol_code, sol_raw if sol_raw is not None else parse_score(sol_out or ""))
    problems = []
    if ws_code == 0:
        problems.append("untouched workspace passed (expected fail)")
    if sol_code != 0:
        problems.append(f"solution overlay failed (exit {sol_code})")
    if sol_code == 0 and sol_score != 1.0:
        problems.append(f"solution score {sol_score} != 1.0")
    if problems:
        detail = "; ".join(problems)
        if ws_out:
            detail += f"\n  ws: {(ws_out or '')[:200]}"
        if sol_out:
            detail += f"\n  sol: {(sol_out or '')[:200]}"
        return False, detail
    return True, f"polarity ok (untouched_score={ws_score}, solution_score={sol_score})"


def import_harbor_tasks(
    from_spec: str,
    out_root: str,
    *,
    collection: str | None = None,
    validate: bool = True,
) -> list[ImportResult]:
    """Import all Harbor tasks from ``from_spec`` into ``out_root``."""
    tasks = discover_harbor_tasks(from_spec)
    out_root = os.path.abspath(out_root)
    results: list[ImportResult] = []
    for harbor_dir in tasks:
        name = os.path.basename(harbor_dir.rstrip(os.sep))
        if collection:
            dest = os.path.join(out_root, collection, name)
        else:
            dest = os.path.join(out_root, name)
        summary = import_task(harbor_dir, dest, collection=collection)
        if summary.ok and validate and summary.solution_materialized:
            vok, detail = validate_imported_task(summary.out_dir)
            summary.validated = vok
            summary.notes.append(detail)
            if not vok:
                summary.needs_manual_attention.append("polarity-failed")
        elif summary.ok and validate and not summary.solution_materialized:
            summary.validated = None
            summary.notes.append("polarity skipped (no solution/)")
        results.append(summary)
    return results


def _print_summary_table(results: list[ImportResult]) -> None:
    cols = (
        "TASK",
        "OK",
        "DOCKER-REQUIRED",
        "SOLUTION",
        "ATTENTION",
        "VALIDATED",
    )
    rows = []
    for r in results:
        rows.append([
            r.task_name,
            "yes" if r.ok and not r.error else "no",
            "yes" if r.docker_required else "no",
            "yes" if r.solution_materialized else "no",
            "yes" if r.needs_manual_attention else "no",
            (
                "yes" if r.validated is True
                else ("no" if r.validated is False else "-")
            ),
        ])
    widths = [len(c) for c in cols]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*cols))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*row))

    n_ok = sum(1 for r in results if r.ok and not r.error)
    n_docker = sum(1 for r in results if r.ok and r.docker_required)
    n_sol = sum(1 for r in results if r.solution_materialized)
    n_attn = sum(1 for r in results if r.needs_manual_attention)
    print()
    print(
        f"summary: imported_ok={n_ok}  docker_required={n_docker}  "
        f"solution_materialized={n_sol}  needs_manual_attention={n_attn}"
    )
    print(
        "License reminder: verify each upstream dataset's license before "
        "redistributing imported tasks."
    )


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="obench import",
        description="Import external task formats into OpenBench.",
    )
    sub = parser.add_subparsers(dest="source", required=True)
    harbor = sub.add_parser(
        "harbor",
        help="import Harbor-format tasks into OpenBench task dirs",
    )
    harbor.add_argument(
        "--from",
        dest="from_spec",
        required=True,
        help="Harbor task directory, parent dataset dir, or glob",
    )
    harbor.add_argument(
        "--out",
        required=True,
        help="output OpenBench tasks root (e.g. tasks-imported)",
    )
    harbor.add_argument(
        "--collection",
        default=None,
        help="optional collection subdirectory under --out",
    )
    harbor.add_argument(
        "--no-validate",
        action="store_true",
        help="skip post-import polarity validation",
    )
    args = parser.parse_args(argv)
    if args.source != "harbor":
        parser.error(f"unknown import source {args.source!r}")

    try:
        results = import_harbor_tasks(
            args.from_spec,
            args.out,
            collection=args.collection,
            validate=not args.no_validate,
        )
    except HarborImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"imported {sum(1 for r in results if r.ok)}/{len(results)} "
        f"Harbor task(s) → {os.path.abspath(args.out)}"
        + (f" (collection={args.collection})" if args.collection else "")
    )
    for r in results:
        if r.error:
            print(f"  FAIL {r.task_name}: {r.error}", file=sys.stderr)
        else:
            flags = []
            if r.docker_required:
                flags.append("DOCKER-REQUIRED")
            if r.solution_materialized:
                flags.append("solution")
            if r.needs_manual_attention:
                flags.append("attention")
            flag_s = f" [{', '.join(flags)}]" if flags else ""
            print(f"  {r.task_name}: {r.out_dir}{flag_s}")
    print()
    _print_summary_table(results)

    clean = [
        r for r in results
        if r.ok
        and not r.error
        and not r.docker_required
        and r.solution_materialized
        and (r.validated is True or args.no_validate)
    ]
    if not clean:
        print(
            "error: zero tasks imported cleanly "
            "(need non-docker-required + solution + polarity pass)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
