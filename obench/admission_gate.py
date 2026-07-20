#!/usr/bin/env python3
"""Pre-admission gate for OpenBench tasks.

No task should enter the dataset unless it passes this automated gate. The gate
covers structural requirements, validate_tasks-style polarity, checker
stress-determinism, basic oracle-ownership heuristics, and timing-sensitivity
scans. Spec-completeness and hack-sweeps remain LLM-judged and are out of scope
for this tool.
"""

import argparse
import ast
import filecmp
import json
import os
import re
import shutil
import sys
import tempfile

from . import determinism_check
from .workspace import (
    WorkspaceError,
    has_git_workspace,
    has_snapshot_workspace,
    materialize_workspace,
)

EXIT_FINDINGS = 3
REQUIRED_ENTRIES = ("instruction.md", "checker.sh", "solution", "PROVENANCE.md")
HARD = "hard"
WARN = "warn"


class Finding:
    def __init__(self, rule, level, message, path=None, detail=None):
        self.rule = rule
        self.level = level
        self.message = message
        self.path = path
        self.detail = detail

    def to_dict(self):
        data = {"rule": self.rule, "level": self.level, "message": self.message}
        if self.path is not None:
            data["path"] = self.path
        if self.detail is not None:
            data["detail"] = self.detail
        return data


def relpath(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def list_files(root):
    files = set()
    if not os.path.isdir(root):
        return files
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            files.add(relpath(os.path.join(dirpath, name), root))
    return files


def file_differs(a, b):
    if not os.path.exists(a) or not os.path.exists(b):
        return True
    if os.path.isdir(a) or os.path.isdir(b):
        return False
    return not filecmp.cmp(a, b, shallow=False)


def compute_deliverables(task_dir, workspace_root=None):
    workspace = workspace_root or os.path.join(task_dir, "workspace")
    solution = os.path.join(task_dir, "solution")
    deliverables = set()
    for rel in list_files(solution):
        ws = os.path.join(workspace, rel)
        sol = os.path.join(solution, rel)
        if not os.path.exists(ws) or file_differs(sol, ws):
            deliverables.add(rel)
    return deliverables


def staged_workspace_root(task_dir):
    """Return ``(workspace_root, temp_dir_or_None)`` for ownership scans.

    Snapshot tasks use ``workspace/`` in place. Git-mode tasks are materialized
    into a disposable temp dir so oracle/ownership heuristics see the same
    starting tree the checker would. Caller must ``shutil.rmtree(temp_dir)``
    when it is not ``None``.
    """
    if has_snapshot_workspace(task_dir):
        return os.path.join(task_dir, "workspace"), None
    if not has_git_workspace(task_dir):
        return os.path.join(task_dir, "workspace"), None
    tmp = tempfile.mkdtemp(prefix="admission-ws-")
    try:
        materialize_workspace(task_dir, tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, tmp


def structure_findings(task_dir):
    findings = []
    for name in REQUIRED_ENTRIES:
        if not os.path.exists(os.path.join(task_dir, name)):
            findings.append(Finding("structure.missing", HARD, f"missing required {name}", name))
    snap = has_snapshot_workspace(task_dir)
    git = has_git_workspace(task_dir)
    if snap and git:
        findings.append(Finding(
            "structure.workspace_conflict", HARD,
            "both workspace/ and workspace.toml present; provide exactly one",
            "workspace.toml",
        ))
    elif not snap and not git:
        findings.append(Finding(
            "structure.missing", HARD,
            "missing required workspace/ or workspace.toml",
            "workspace",
        ))
    return findings


def checker_data_nonempty(task_dir):
    """checker_data/ is required only when checker.sh references it.

    A self-contained checker.sh (oracle inline, no checker_data reads) still
    satisfies oracle ownership.
    """
    checker_data = os.path.join(task_dir, "checker_data")
    referenced = False
    checker_sh = os.path.join(task_dir, "checker.sh")
    try:
        with open(checker_sh, "r", encoding="utf-8", errors="replace") as fh:
            referenced = "checker_data" in fh.read()
    except OSError:
        pass
    if not os.path.isdir(checker_data):
        if referenced:
            return [Finding("ownership.checker_data_missing", HARD, "checker.sh references checker_data/ but it is missing", "checker_data")]
        return []
    if not any(files for _root, _dirs, files in os.walk(checker_data)):
        if referenced:
            return [Finding("ownership.checker_data_empty", HARD, "checker.sh references checker_data/ but it is empty", "checker_data")]
        return []
    return []


def validate_equivalence(task_dir):
    """Bounded validate_tasks-style polarity check for one task."""
    missing = [name for name in ("solution", "checker.sh") if not os.path.exists(os.path.join(task_dir, name))]
    snap = has_snapshot_workspace(task_dir)
    git = has_git_workspace(task_dir)
    if snap and git:
        missing.append("workspace xor workspace.toml")
    elif not snap and not git:
        missing.append("workspace/ or workspace.toml")
    if missing:
        return [Finding("validate_tasks.skipped", HARD, "cannot run checker polarity; missing " + ", ".join(missing))], None
    try:
        ws = determinism_check.run_checker_once(task_dir, overlay_solution=False)
        sol = determinism_check.run_checker_once(task_dir, overlay_solution=True)
    except Exception as exc:
        return [Finding("validate_tasks.error", HARD, f"validate_tasks equivalence crashed: {exc}")], None

    findings = []
    if ws["timed_out"] or sol["timed_out"]:
        findings.append(Finding("validate_tasks.timeout", HARD, "checker timed out during workspace/solution polarity check"))
    if ws["exit_code"] == 0:
        findings.append(Finding("validate_tasks.workspace_passed", HARD, "bare workspace checker passed; expected FAIL", detail=ws["output"]))
    if sol["exit_code"] != 0:
        findings.append(Finding("validate_tasks.solution_failed", HARD, "golden solution checker failed; expected PASS", detail=sol["output"]))
    if sol["exit_code"] == 0 and sol["parsed_score"] is not None and abs(sol["parsed_score"] - 1.0) > 1e-9:
        findings.append(Finding("validate_tasks.solution_score", HARD, f"solution exited 0 but SCORE={sol['parsed_score']:.3f}; expected 1.0"))
    summary = {
        "workspace_exit": ws["exit_code"],
        "solution_exit": sol["exit_code"],
        "workspace_score": ws["score"],
        "solution_score": sol["score"],
    }
    return findings, summary


class PathReadVisitor(ast.NodeVisitor):
    def __init__(self):
        self.reads = []
        self.import_execs = []
        self.time_warnings = []
        self.constants = {}
        self._sleep_lines = []
        self._signal_lines = []

    def _literal_path(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                else:
                    parts.append("*")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._literal_path(node.left)
            right = self._literal_path(node.right)
            if left is not None and right is not None:
                return left + right
        if isinstance(node, ast.Call) and self._func_name(node.func).endswith("Path") and node.args:
            return self._literal_path(node.args[0])
        return None

    def _func_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._func_name(node.value)
            return base + "." + node.attr if base else node.attr
        return ""

    def _collect_subprocess_paths(self, node):
        if not node.args:
            return []
        arg = node.args[0]
        values = []
        if isinstance(arg, (ast.List, ast.Tuple)):
            for item in arg.elts:
                path = self._literal_path(item)
                if path:
                    values.append(path)
        else:
            path = self._literal_path(arg)
            if path:
                values.append(path)
        return values

    def visit_Assign(self, node):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.constants[target.id] = node.value.value
        self.generic_visit(node)

    def _numeric_value(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            return self.constants.get(node.id)
        return None

    def visit_Call(self, node):
        name = self._func_name(node.func)
        lineno = getattr(node, "lineno", None)
        if name.endswith("open") and node.args:
            mode = self._literal_path(node.args[1]) if len(node.args) > 1 else None
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode = self._literal_path(kw.value)
            if mode is None or any(flag in mode for flag in ("r", "+")):
                path = self._literal_path(node.args[0])
                if path:
                    self.reads.append((lineno, path, "open"))
        if name.endswith(("read_text", "read_bytes")):
            path = self._path_from_value(node.func.value if isinstance(node.func, ast.Attribute) else None)
            if path:
                self.reads.append((lineno, path, name))
        if name.endswith(("subprocess.run", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen")):
            paths = self._collect_subprocess_paths(node)
            for path in paths:
                self.reads.append((lineno, path, name))
                if path.endswith(".py"):
                    self.import_execs.append((lineno, path, name))
            for kw in node.keywords:
                timeout_value = self._numeric_value(kw.value) if kw.arg == "timeout" else None
                if timeout_value is not None and timeout_value < 30:
                    self.time_warnings.append((lineno, f"subprocess timeout={timeout_value:g} < 30"))
        if name.endswith("spec_from_file_location"):
            for arg in node.args[1:2]:
                path = self._literal_path(arg)
                if path:
                    self.import_execs.append((lineno, path, name))
        if name.endswith(("SourceFileLoader", "run_path")):
            for arg in node.args:
                path = self._literal_path(arg)
                if path and path.endswith(".py"):
                    self.import_execs.append((lineno, path, name))
        if name.endswith("time.sleep") or name == "sleep":
            self._sleep_lines.append(lineno)
        if "signal" in name or name in {"kill", "terminate", "send_signal", "killpg"}:
            self._signal_lines.append(lineno)
        self.generic_visit(node)

    def visit_Compare(self, node):
        text = ast.unparse(node) if hasattr(ast, "unparse") else "comparison"
        if re.search(r"elapsed|duration|wall|monotonic|perf_counter|time\(\)", text, re.I):
            for comp in list(node.comparators) + [node.left]:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, (int, float)) and comp.value < 30:
                    self.time_warnings.append((getattr(node, "lineno", None), f"wall-clock elapsed assertion bound < 30: {text}"))
        self.generic_visit(node)

    def _path_from_value(self, node):
        if isinstance(node, ast.Call) and self._func_name(node.func).endswith("Path") and node.args:
            return self._literal_path(node.args[0])
        return self._literal_path(node)

    def finalize(self):
        for sleep_line in self._sleep_lines:
            if any(sig_line is not None and sleep_line is not None and 0 <= sig_line - sleep_line <= 30 for sig_line in self._signal_lines):
                self.time_warnings.append((sleep_line, "time.sleep followed by signal/kill call"))
                break


def is_probably_text(path):
    try:
        with open(path, "rb") as fh:
            sample = fh.read(4096)
    except OSError:
        return False
    return b"\0" not in sample


def source_files(task_dir):
    paths = [os.path.join(task_dir, "checker.sh")]
    checker_data = os.path.join(task_dir, "checker_data")
    if os.path.isdir(checker_data):
        for dirpath, dirnames, filenames in os.walk(checker_data):
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            for name in filenames:
                path = os.path.join(dirpath, name)
                if name.endswith((".pyc", ".pyo")):
                    continue
                if is_probably_text(path):
                    paths.append(path)
    return [p for p in paths if os.path.exists(p)]


def read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except UnicodeDecodeError:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()


def literal_paths_from_shell(text):
    reads = []
    execs = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in re.finditer(r"(cat|diff|cmp|python3?|bash|sh|grep|awk|sed|node)\s+([^;&|\n]+)", line):
            command = match.group(1)
            for token in re.findall(r"['\"]([^'\"]+)['\"]|(\S+)", match.group(2)):
                value = token[0] or token[1]
                if not value or value.startswith("-"):
                    continue
                reads.append((lineno, value, "shell"))
                if command.startswith("python") and value.endswith(".py"):
                    execs.append((lineno, value, "shell-python"))
        for match in re.finditer(r"(?:<|source\s+)\s*['\"]?([^'\"\s;&|]+)", line):
            reads.append((lineno, match.group(1), "shell"))
    return reads, execs


def normalize_candidate_path(raw):
    raw = raw.strip().strip('"\'')
    raw = raw.replace("${PWD}", ".").replace("$PWD", ".")
    raw = raw.replace("${TASK_DIR}", "TASK_DIR").replace("$TASK_DIR", "TASK_DIR")
    raw = raw.replace("./", "", 1) if raw.startswith("./") else raw
    markers = ["workspace/", "workdir/", "/workspace/"]
    for marker in markers:
        if marker in raw:
            return raw.split(marker, 1)[1]
    if raw.startswith("TASK_DIR/") or raw.startswith("checker_data/"):
        return None
    if raw.startswith("/"):
        return None
    return raw


def scan_ownership(task_dir):
    findings = []
    tmp = None
    try:
        try:
            workspace_root, tmp = staged_workspace_root(task_dir)
        except WorkspaceError as exc:
            findings.append(Finding(
                "ownership.workspace_materialize",
                HARD,
                f"cannot materialize git workspace for ownership scan: {exc}",
                "workspace.toml",
            ))
            return findings
        except Exception as exc:  # noqa: BLE001 - surface staging crashes as findings
            findings.append(Finding(
                "ownership.workspace_materialize",
                HARD,
                f"cannot materialize git workspace for ownership scan: {exc}",
                "workspace.toml",
            ))
            return findings

        workspace_files = list_files(workspace_root)
        deliverables = compute_deliverables(task_dir, workspace_root=workspace_root)
        allowed = set(deliverables)
        for path in source_files(task_dir):
            rel = relpath(path, task_dir)
            text = read_text(path)
            read_refs = []
            import_refs = []
            time_warnings = []
            if path.endswith(".py"):
                try:
                    tree = ast.parse(text, filename=path)
                    visitor = PathReadVisitor()
                    visitor.visit(tree)
                    visitor.finalize()
                    read_refs = visitor.reads
                    import_refs = visitor.import_execs
                    time_warnings = visitor.time_warnings
                except SyntaxError as exc:
                    findings.append(Finding("scan.syntax", WARN, f"could not parse {rel}: {exc}", rel))
            else:
                read_refs, import_refs = literal_paths_from_shell(text)
                if re.search(r"timeout\s+[0-2]?\d(?:\D|$)", text):
                    time_warnings.append((None, "shell timeout command with bound < 30"))

            # Regex fallback catches literal workspace paths in dynamic Python too.
            for lineno, line in enumerate(text.splitlines(), 1):
                if "workspace/" in line or "/workspace/" in line:
                    for match in re.finditer(r"(?:workspace/|/workspace/)([A-Za-z0-9_./-]+)", line):
                        read_refs.append((lineno, match.group(1), "literal-workspace"))
                if re.search(r"time\.sleep\([^)]*\)", line):
                    following = "\n".join(text.splitlines()[lineno:lineno + 30])
                    if re.search(r"signal|kill|terminate|send_signal", following):
                        time_warnings.append((lineno, "time.sleep followed by signal/kill call"))
                if re.search(r"timeout\s*=\s*([0-9]+(?:\.[0-9]+)?)", line):
                    value = float(re.search(r"timeout\s*=\s*([0-9]+(?:\.[0-9]+)?)", line).group(1))
                    if value < 30:
                        time_warnings.append((lineno, f"subprocess timeout={value:g} < 30"))

            for lineno, raw, how in read_refs:
                norm = normalize_candidate_path(raw)
                if norm in workspace_files and norm not in allowed:
                    findings.append(Finding(
                        "ownership.workspace_read",
                        WARN,
                        f"checker appears to read non-deliverable workspace file {norm!r} via {how}",
                        rel,
                        {"line": lineno, "path": norm},
                    ))
            for lineno, raw, how in import_refs:
                norm = normalize_candidate_path(raw)
                explicit_workspace = "workspace/" in raw or "/workspace/" in raw or "workdir/" in raw
                if norm in workspace_files and norm.endswith(".py") and norm not in allowed and (how != "shell-python" or explicit_workspace):
                    findings.append(Finding(
                        "ownership.workspace_py_reference",
                        HARD,
                        f"checker imports or executes workspace Python file {norm!r} as reference via {how}",
                        rel,
                        {"line": lineno, "path": norm},
                    ))
            for lineno, message in time_warnings:
                findings.append(Finding("timing_sensitivity", HARD, message, rel, {"line": lineno}))
        return findings
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def run_determinism(task_dir, runs=determinism_check.DEFAULT_RUNS, stress=determinism_check.DEFAULT_STRESS):
    try:
        result = determinism_check.run_determinism_check(task_dir, runs=runs, stress=stress)
    except Exception as exc:
        return [Finding("determinism.error", HARD, f"determinism check crashed: {exc}")], None
    if not result.get("pass"):
        return [Finding("determinism.failed", HARD, "checker verdicts diverged under stress", detail=result.get("findings"))], result
    return [], result


def gate(task_path, determinism_runs=determinism_check.DEFAULT_RUNS, stress=determinism_check.DEFAULT_STRESS):
    task_dir = os.path.abspath(task_path)
    findings = []
    findings.extend(structure_findings(task_dir))
    findings.extend(checker_data_nonempty(task_dir))
    vt_findings, vt_summary = validate_equivalence(task_dir)
    findings.extend(vt_findings)
    findings.extend(scan_ownership(task_dir))
    det_findings, det_summary = run_determinism(task_dir, runs=determinism_runs, stress=stress)
    findings.extend(det_findings)

    hard = any(f.level == HARD for f in findings)
    warn = any(f.level == WARN for f in findings)
    status = "FAIL" if hard else ("PASS-WITH-WARNINGS" if warn else "PASS")
    wall = (det_summary or {}).get("wall_time_s")
    return {
        "task": task_dir,
        "status": status,
        "pass": status != "FAIL",
        "findings": [f.to_dict() for f in findings],
        "validate_tasks": vt_summary,
        "determinism": det_summary,
        "checker_wall_time_s": wall,
    }


def print_human(result):
    print(f"admission_gate: {result['status']} {result['task']}")
    if result.get("validate_tasks"):
        vt = result["validate_tasks"]
        print(f"validate_tasks: workspace_exit={vt['workspace_exit']} solution_exit={vt['solution_exit']}")
    if result.get("checker_wall_time_s"):
        all_times = result["checker_wall_time_s"].get("all", {})
        if all_times.get("min") is not None:
            print(f"checker wall time: min={all_times['min']:.3f}s median={all_times['median']:.3f}s max={all_times['max']:.3f}s")
    for item in result["findings"]:
        loc = f" [{item['path']}]" if "path" in item else ""
        print(f"{item['level'].upper()}: {item['rule']}{loc}: {item['message']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = gate(args.task_path)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_human(result)
    return 0 if result["status"] != "FAIL" else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
