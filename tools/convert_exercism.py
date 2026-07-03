#!/usr/bin/env python3
"""Convert an Exercism exercise into an OpenBench task directory.

Usage:
    python3 tools/convert_exercism.py <slug> [<slug> ...]
    python3 tools/convert_exercism.py --all

For each slug it fetches the upstream canonical data (the test cases) and the
description from the MIT-licensed ``exercism/problem-specifications`` repo, then
emits a task under ``tasks-imported/exercism/<slug>/`` in OpenBench's task
contract:

    instruction.md        our own prose + a generated interface section
    workspace/solution.py  a stub the agent fills in
    workspace/README.md    names the entry point
    checker_data/cases.json  the flattened canonical cases (snake_cased inputs)
    checker_data/run_score.py  the generic scorer (identical for every task)
    checker.sh            execs the scorer
    provenance.json       upstream URLs, license, and retrieval timestamp

What this tool does NOT do: it never copies upstream prose into instruction.md
(the text is written from scratch in exercism_registry.py) and it never writes
``solution/`` — the reference solution is authored by hand, per task, and must
not be derived from any Exercism track solution.

The canonical data is the single source of the graded cases; the reference
solution under ``solution/`` proves the task is solvable. ``validate_tasks.py``
checks that the stub workspace fails and the reference solution scores 1.0.

Standard library only. Needs network only at convert time (never at run time).
"""
import argparse
import datetime
import json
import os
import re
import stat
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT_ROOT = os.path.join(REPO, "tasks-imported", "exercism")

sys.path.insert(0, HERE)
from exercism_registry import REGISTRY  # noqa: E402

RAW = "https://raw.githubusercontent.com/exercism/problem-specifications/main/exercises/{slug}/{f}"
UPSTREAM = "https://github.com/exercism/problem-specifications/tree/main/exercises/{slug}"


def fetch(slug, filename):
    url = RAW.format(slug=slug, f=filename)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return url, resp.read().decode("utf-8")


def fetch_description(slug):
    """The upstream prose file is named inconsistently; try the known names.

    We fetch it only to record its URL in provenance (for attribution/audit) --
    its text is never reused. Returns the URL of the first one that exists.
    """
    for name in ("instructions.md", "description.md", "introduction.md"):
        try:
            url, _ = fetch(slug, name)
            return url
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    return UPSTREAM.format(slug=slug)


def snake(name):
    """camelCase / PascalCase -> snake_case (leaves already-snake names alone)."""
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", name)
    return s.lower()


def flatten(cases):
    """Yield the leaf cases of a canonical-data tree (skipping deprecated ones)."""
    out = []
    for case in cases:
        if case.get("deprecated"):
            continue
        if "cases" in case:
            out.extend(flatten(case["cases"]))
        else:
            out.append(case)
    return out


def build_cases(slug, spec, canonical):
    """Turn canonical leaves into the scorer's case list, validating the interface."""
    roundtrip = spec.get("roundtrip", {})
    functions = spec["functions"]
    cases = []
    for leaf in flatten(canonical["cases"]):
        prop = leaf["property"]
        desc = leaf.get("description", prop)
        kwargs = {snake(k): v for k, v in leaf.get("input", {}).items()}
        expected = leaf.get("expected")

        if prop in roundtrip:
            cases.append({"roundtrip": roundtrip[prop], "kwargs": kwargs,
                          "expected": expected, "desc": desc})
            continue

        if prop not in functions:
            raise SystemExit(
                "{}: canonical property {!r} is not mapped in the registry"
                .format(slug, prop))
        fn = functions[prop]
        allowed = set(fn["params"])
        extra = set(kwargs) - allowed
        if extra:
            raise SystemExit(
                "{}: case input keys {} exceed declared params {} for {}"
                .format(slug, sorted(extra), sorted(allowed), fn["name"]))

        if isinstance(expected, dict) and "error" in expected:
            cases.append({"fn": fn["name"], "kwargs": kwargs, "error": True, "desc": desc})
        else:
            cases.append({"fn": fn["name"], "kwargs": kwargs, "expected": expected, "desc": desc})
    return cases


def ordered_functions(spec):
    """Interface functions in a stable order (dict insertion order of the registry)."""
    seen = {}
    for fn in spec["functions"].values():
        seen[fn["name"]] = fn
    return list(seen.values())


def render_instruction(spec):
    lines = ["# {}".format(spec["title"]), "", spec["prose"].rstrip(), "",
             "## Interface", "",
             "Put your solution in a file named `solution.py` in this directory, "
             "exposing:", ""]
    for fn in ordered_functions(spec):
        sig = "{}({})".format(fn["name"], ", ".join(fn["params"]))
        lines.append("- `{}` — {}".format(sig, fn["doc"]))
    if spec.get("raises"):
        lines += ["", spec["raises"]]
    lines += ["", "Done when `solution.py` implements the interface above exactly "
              "as specified."]
    return "\n".join(lines) + "\n"


def render_stub(spec):
    lines = ['"""Implement the interface described in instruction.md."""', ""]
    for fn in ordered_functions(spec):
        lines.append("def {}({}):".format(fn["name"], ", ".join(fn["params"])))
        lines.append('    """{}"""'.format(fn["doc"][0].upper() + fn["doc"][1:]))
        lines.append("    raise NotImplementedError")
        lines.append("")
    return "\n".join(lines)


def render_readme(spec):
    names = ", ".join("`{}`".format(fn["name"]) for fn in ordered_functions(spec))
    return ("# Workspace\n\nImplement your solution in `solution.py` in this "
            "directory. The entry point is the function(s) {} described in "
            "`instruction.md`.\n".format(names))


SCORER = r'''#!/usr/bin/env python3
"""Generic scorer for imported Exercism tasks.

Runs the agent's ``solution.py`` (found in the current working directory, which
the runner sets to a fresh copy of the task workspace) against the flattened
canonical cases in ``cases.json`` shipped next to this script. Each case is run
in isolation so one broken function only fails its own cases; the denominator
stays fixed. Prints per-failure lines, a summary, and a final ``SCORE:`` line;
exits 0 only when every case passes.

Case shapes in cases.json:
  {"fn": "<name>", "kwargs": {...}, "expected": <value>}   fn(**kwargs) == expected
  {"fn": "<name>", "kwargs": {...}, "error": true}         fn(**kwargs) must raise ValueError
  {"roundtrip": ["<outer>", "<inner>"], "kwargs": {...}, "expected": <value>}
                                                           outer(inner(**kwargs)) == expected
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_solution():
    path = os.path.join(os.getcwd(), "solution.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("agent_solution", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_case(mod, case):
    if "roundtrip" in case:
        outer, inner = case["roundtrip"]
        return getattr(mod, outer)(getattr(mod, inner)(**case["kwargs"])) == case["expected"]
    fn = getattr(mod, case["fn"], None)
    if not callable(fn):
        return False
    try:
        got = fn(**case["kwargs"])
    except ValueError:
        return bool(case.get("error"))
    if case.get("error"):
        return False
    return got == case["expected"]


def main():
    with open(os.path.join(HERE, "cases.json"), encoding="utf-8") as fh:
        cases = json.load(fh)
    mod = load_solution()

    total = len(cases)
    passed = 0
    failures = []
    for i, case in enumerate(cases):
        ok = False
        if mod is not None:
            try:
                ok = run_case(mod, case)
            except Exception:
                ok = False
        if ok:
            passed += 1
        else:
            failures.append(case.get("desc", "case {}".format(i)))

    for desc in failures[:50]:
        print("FAIL {}".format(desc))
    print("{}/{} cases passing".format(passed, total))
    print("SCORE: {:.4f}".format((passed / total) if total else 0.0))
    return 0 if (total and passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


CHECKER = '''#!/usr/bin/env bash
# Scores solution.py against the imported canonical cases. Prints a SCORE line
# and exits 0 only when every case passes. Runs with cwd = the agent's
# workspace copy; the scorer and cases live under $TASK_DIR/checker_data.
set -uo pipefail

exec python3 "$TASK_DIR/checker_data/run_score.py"
'''


def write(path, content, executable=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if executable:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def convert(slug):
    if slug not in REGISTRY:
        raise SystemExit("no registry entry for {!r}".format(slug))
    spec = REGISTRY[slug]

    data_url, data_text = fetch(slug, "canonical-data.json")
    desc_url = fetch_description(slug)  # fetched for provenance/audit only
    canonical = json.loads(data_text)
    cases = build_cases(slug, spec, canonical)

    out = os.path.join(OUT_ROOT, slug)
    write(os.path.join(out, "instruction.md"), render_instruction(spec))
    write(os.path.join(out, "workspace", "solution.py"), render_stub(spec))
    write(os.path.join(out, "workspace", "README.md"), render_readme(spec))
    write(os.path.join(out, "checker_data", "cases.json"),
          json.dumps(cases, indent=2, ensure_ascii=False) + "\n")
    write(os.path.join(out, "checker_data", "run_score.py"), SCORER)
    write(os.path.join(out, "checker.sh"), CHECKER, executable=True)

    provenance = {
        "source": "exercism/problem-specifications",
        "exercise": slug,
        "license": "MIT",
        "canonical_data_url": data_url,
        "description_url": desc_url,
        "upstream_dir": UPSTREAM.format(slug=slug),
        "retrieved_utc": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0).isoformat(),
        "note": ("Graded cases are derived from the upstream canonical data. "
                 "Task prose and the reference solution are original OpenBench "
                 "work, not copied from Exercism."),
    }
    write(os.path.join(out, "provenance.json"),
          json.dumps(provenance, indent=2) + "\n")

    print("wrote {}  ({} cases)".format(os.path.relpath(out, REPO), len(cases)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slugs", nargs="*", help="exercise slug(s) to convert")
    ap.add_argument("--all", action="store_true",
                    help="convert every slug in the registry")
    args = ap.parse_args()

    slugs = list(REGISTRY) if args.all else args.slugs
    if not slugs:
        ap.error("give one or more slugs, or --all")
    for slug in slugs:
        convert(slug)


if __name__ == "__main__":
    main()
