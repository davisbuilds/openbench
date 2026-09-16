#!/usr/bin/env python3
"""Offline Docker admission probe for the fork-local Dojo Harbor package.

No model, credentials, host mounts, or Harbor installation required. Image build
may download pinned dependencies; execution uses Docker's network=none. This
proves this image/launch boundary, not a future Harbor or authenticated launch.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid


REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "harbor-tasks-local/dojo-evidence-pr60-v2"
SOURCE = REPO / "tasks-local/dojo-evidence-pr60"


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def manifest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def validate_package() -> dict[str, str]:
    """Package parity is required before exercising either positive control."""
    source = manifest(SOURCE)
    for source_dir, packaged_dir in (("workspace", "environment/app"),):
        assert manifest(SOURCE / source_dir) == manifest(TASK / packaged_dir), source_dir
    original_checks = manifest(SOURCE / "checker_data")
    corrected_checks = manifest(TASK / "tests/checker_data")
    assert original_checks.keys() == corrected_checks.keys()
    assert original_checks.pop("oracle.py") != corrected_checks.pop("oracle.py")
    assert original_checks == corrected_checks
    expected_solution = manifest(SOURCE / "solution")
    actual_solution = manifest(TASK / "solution")
    actual_solution.pop("solve.sh")
    assert expected_solution == actual_solution
    for original, packaged in (("checker.sh", "tests/checker.sh"),
                               ("instruction.md", "instruction.md")):
        assert (SOURCE / original).read_bytes() == (TASK / packaged).read_bytes()
    assert not any(p.is_symlink() for p in TASK.rglob("*")), "package symlink"
    return source


def replace_function(source: str, replacement: str, name: str) -> str:
    def location(text):
        return next(n for n in ast.parse(text).body
                    if isinstance(n, ast.FunctionDef) and n.name == name)
    old, new = location(source), location(replacement)
    lines, new_lines = source.splitlines(keepends=True), replacement.splitlines(keepends=True)
    return "".join(lines[:old.lineno - 1] + new_lines[new.lineno - 1:new.end_lineno]
                   + lines[old.end_lineno:])


def control_overlay(directory: Path, kind: str) -> None:
    profiles = directory / "scripts/profiles"
    profiles.mkdir(parents=True)
    fixed = TASK / "solution/scripts/profiles"
    buggy = SOURCE / "workspace/scripts/profiles"
    rollout = (fixed / "rollout_codex.py").read_text()
    budget = (fixed / "budget.py").read_text()
    if kind.startswith("partial-"):
        mask = kind.removeprefix("partial-")
        if mask[0] == "0":
            rollout = replace_function(rollout, (buggy / "rollout_codex.py").read_text(),
                                       "read_rollout")
        if mask[1] == "0":
            budget = (buggy / "budget.py").read_text()
        if mask[2] == "0":
            rollout = replace_function(rollout, (buggy / "rollout_codex.py").read_text(),
                                       "surface_mismatch")
    else:
        # Independent valid presentation: retain qualified multiset comparison,
        # but show the legacy bare skill names in both diagnostic lists.
        for key, counter in (("only_in_recorded", "only_recorded"),
                             ("only_in_live", "only_live")):
            old = f'"{key}": sorted({counter}.elements()),'
            new = f'"{key}": sorted(item.rsplit(":", 1)[-1] for item in {counter}.elements()),'
            assert rollout.count(old) == 1
            rollout = rollout.replace(old, new)
    (profiles / "rollout_codex.py").write_text(rollout)
    (profiles / "budget.py").write_text(budget)


def main() -> None:
    if not __debug__:
        raise RuntimeError("Run without -O: this admission probe requires assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="openbench-local/dojo-evidence-pr60:probe")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    frozen_source = validate_package()
    if not args.skip_build:
        subprocess.run(["docker", "build", "--tag", args.image,
                        str(TASK / "environment")], check=True)
    receipt = {"schema": 1, "task": "dojo-evidence-pr60-v2", "started_epoch": time.time(),
               "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "scope": "offline Docker image and explicit launch; not Harbor/auth admission",
               "image_id": run("docker", "image", "inspect", args.image,
                               "--format", "{{.Id}}").stdout.strip(),
               "docker_version": run("docker", "version", "--format", "{{json .}}").stdout.strip(),
               "package_manifest": manifest(TASK), "attempts": []}
    with tempfile.TemporaryDirectory(prefix="dojo-boundary-") as temporary:
        scratch = Path(temporary)
        canary = scratch / "host-only.txt"
        canary.write_text("host-only-" + uuid.uuid4().hex)
        before = canary.read_bytes()
        partials = ["partial-" + "".join(mask) for mask in itertools.product("01", repeat=3)
                    if mask not in (("0", "0", "0"), ("1", "1", "1"))]
        cases = ["deliberately-exposed-control", "buggy", "reference",
                 "alternative-v1-control", "alternative", *partials]
        for kind in cases:
            name = "ob-dojo-probe-" + uuid.uuid4().hex[:12]
            try:
                run("docker", "run", "--detach", "--name", name, "--network", "none",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    args.image, "sleep", "300")
                inspect = json.loads(run("docker", "inspect", name).stdout)[0]
                assert inspect["Mounts"] == [], "unexpected host/volume mount"
                assert inspect["HostConfig"]["NetworkMode"] == "none"
                image_workspace = json.loads(run("docker", "exec", name, "python3", "-c",
                    "import hashlib,json,pathlib; r=pathlib.Path('/app'); "
                    "print(json.dumps({str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() "
                    "for p in r.rglob('*') if p.is_file()}))").stdout)
                assert image_workspace == manifest(TASK / "environment/app"), "stale image workspace"
                if kind == "deliberately-exposed-control":
                    # The exact same denial instrument must reject an exposed
                    # canary; a probe that always reports absence cannot pass.
                    run("docker", "exec", name, "mkdir", "-p", str(canary.parent))
                    run("docker", "cp", str(canary), name + ":" + str(canary))
                probe = r'''
import json, pathlib, subprocess, sys
import yaml
assert yaml.__version__ == '6.0.3'
assert pathlib.Path('/app/scripts/profiles/rollout_codex.py').is_file()
assert not any(pathlib.Path('/app').rglob('.git'))
assert not any(pathlib.Path('/app').rglob('checker_data'))
assert not pathlib.Path('/tests').exists()
assert not pathlib.Path('/solution').exists()
canary = pathlib.Path(sys.argv[1])
known_host_solution = pathlib.Path(sys.argv[2])
positive = pathlib.Path('/app/probe-positive.txt')
positive.write_text('workspace-write-control')
assert subprocess.check_output([sys.executable, '-c',
    'import pathlib; print(pathlib.Path("/app/probe-positive.txt").read_text())'],
    text=True).strip() == 'workspace-write-control'
link = pathlib.Path('/app/host-symlink')
link.symlink_to(canary)
def denied(path):
    try: path.read_bytes()
    except (FileNotFoundError, PermissionError): return True
    return False
checks = {'direct': denied(canary), 'symlink': denied(link),
          'host_solution': denied(known_host_solution)}
child = subprocess.run([sys.executable, '-c',
    'from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()', str(canary)],
    capture_output=True)
checks['subprocess'] = child.returncode != 0
link.unlink()
print(json.dumps(checks))
sys.exit(0 if all(checks.values()) else 1)
'''
                probe_result = subprocess.run(
                    ["docker", "exec", "-i", name, "python3", "-", str(canary),
                     str(SOURCE / "solution/scripts/profiles/rollout_codex.py")],
                    input=probe, text=True, capture_output=True)
                probe_checks = json.loads(probe_result.stdout)
                expected_pass = kind != "deliberately-exposed-control"
                assert (probe_result.returncode == 0) == expected_pass, probe_result
                if not expected_pass:
                    assert probe_checks["direct"] is False
                    assert probe_checks["symlink"] is False
                    assert probe_checks["subprocess"] is False
                # Observe the successful write from the host, independently of
                # the constrained process's claim that it could write/read it.
                observed = scratch / (kind + "-positive.txt")
                run("docker", "cp", name + ":/app/probe-positive.txt", str(observed))
                assert observed.read_text() == "workspace-write-control"
                assert canary.read_bytes() == before, "host canary changed"
                attempt = {"kind": kind, "boundary": probe_checks,
                           "mounts": inspect["Mounts"], "network": "none",
                           "positive_write_observed": True, "host_canary_unchanged": True}
                if expected_pass:
                    # Agent phase has ended. Only now inject verifier files.
                    # Reference oracle is a separate run, never an agent run.
                    if kind == "reference":
                        run("docker", "cp", str(TASK / "solution"), name + ":/solution")
                        run("docker", "exec", name, "bash", "/solution/solve.sh")
                    elif kind.startswith(("alternative", "partial-")):
                        overlay = scratch / (kind + "-overlay")
                        control_overlay(overlay, kind)
                        run("docker", "cp", str(overlay / "scripts"), name + ":/app/")
                    tests = TASK / "tests"
                    if kind == "alternative-v1-control":
                        tests = scratch / "v1-tests"
                        shutil.copytree(TASK / "tests", tests)
                        shutil.copy2(SOURCE / "checker_data/oracle.py", tests / "checker_data/oracle.py")
                    run("docker", "cp", str(tests), name + ":/tests")
                    run("docker", "exec", name, "mkdir", "-p", "/logs/verifier")
                    verifier = run("docker", "exec", name, "bash", "/tests/test.sh")
                    logs = scratch / (kind + "-logs")
                    run("docker", "cp", name + ":/logs/verifier", str(logs))
                    evidence = json.loads((logs / "openbench-verifier-evidence.json").read_text())
                    reward = float((logs / "reward.txt").read_text())
                    if kind.startswith("partial-"):
                        expected_reward = round(kind.removeprefix("partial-").count("1") / 3, 4)
                    else:
                        expected_reward = {"buggy": 0.0, "reference": 1.0,
                                           "alternative": 1.0, "alternative-v1-control": 0.6667}[kind]
                    assert reward == expected_reward, (kind, verifier.stdout)
                    assert evidence["checker_exit"] == (0 if reward == 1.0 else 1)
                    attempt.update(reward=reward, evidence=evidence,
                                   verifier_stdout=verifier.stdout, verifier_stderr=verifier.stderr)
                receipt["attempts"].append(attempt)
            finally:
                subprocess.run(["docker", "rm", "--force", name], check=False,
                               capture_output=True)
        assert canary.read_bytes() == before
    assert manifest(SOURCE) == frozen_source, "original task changed during probe"
    assert manifest(TASK) == receipt["package_manifest"], "package changed during probe"
    receipt["finished_epoch"] = time.time()
    receipt["source_unchanged"] = True
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"receipt": str(args.receipt), "image_id": receipt["image_id"],
                      "buggy_reward": 0.0, "reference_reward": 1.0,
                      "boundary": "passed, including exposed negative control"}))


if __name__ == "__main__":
    main()
