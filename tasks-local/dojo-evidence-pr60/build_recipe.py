"""Rebuild source artifacts from pinned Dojo Git objects; no checkout mutation."""
import ast
import io
from pathlib import Path
import subprocess
import sys
import tokenize

PRE = "e0163e0fc489e6fee0b469a84fe80c2116e2ddda"
FINAL = "6250a0b152eec023678d0da53fa6850536fba954"


def show(repo, rev, path):
    return subprocess.check_output(["git", "-C", str(repo), "show", f"{rev}:{path}"]).decode()


def sanitize(source):
    """Blank narrative docstrings/comments without changing executable statements."""
    lines = source.splitlines(keepends=True)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                if any(word in first.value.value.lower() for word in ("copyright", "spdx", "licensed under")):
                    continue
                indent = " " * first.col_offset
                lines[first.lineno - 1] = indent + '""" """\n'
                for i in range(first.lineno, first.end_lineno):
                    lines[i] = "\n"
    tokens = []
    for token in tokenize.generate_tokens(io.StringIO("".join(lines)).readline):
        if token.type == tokenize.COMMENT and not any(k in token.string.lower() for k in ("copyright", "license", "spdx")):
            token = token._replace(string="")
        tokens.append(token)
    clean = "\n".join(line.rstrip() for line in tokenize.untokenize(tokens).splitlines()).rstrip() + "\n"
    ast.parse(clean)
    return clean


def build(repo, target):
    paths = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", FINAL, "scripts/profiles"]).decode().splitlines()
    for path in paths:
        if not path.endswith(".py"):
            continue
        source = show(repo, FINAL, path)
        if path.endswith("/rollout_codex.py"):
            final_source = source
            source = show(repo, PRE, path)
            # The pre file also predates observations(errors=...). Preserve that
            # unrelated final API/error-handling behavior rather than add a
            # fourth, unscored regression to the task.
            old = next(n for n in ast.parse(source).body
                       if isinstance(n, ast.FunctionDef) and n.name == "observations")
            new = next(n for n in ast.parse(final_source).body
                       if isinstance(n, ast.FunctionDef) and n.name == "observations")
            lines = source.splitlines(keepends=True)
            replacement = final_source.splitlines(keepends=True)[new.lineno - 1:new.end_lineno]
            source = "".join(lines[:old.lineno - 1] + replacement + lines[old.end_lineno:])
        if path.endswith("/budget.py"):
            guard = "            and self.policy.accepts_surface(self.surface)\n"
            assert source.count(guard) == 1
            source = source.replace(guard, "")
            start = source.index("    if not policy.accepts_surface(surface):", source.index("def assess("))
            end = source.index("    if not entries:", start)
            source = source[:start] + source[end:]
        dest = target / "workspace" / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(sanitize(source))
    for name in ("rollout_codex.py", "budget.py"):
        dest = target / "solution/scripts/profiles" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(show(repo, FINAL, "scripts/profiles/" + name))
    (target / "workspace/requirements.txt").write_text("PyYAML==6.0.3\n")


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(__file__).resolve().parent)
