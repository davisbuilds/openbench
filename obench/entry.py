#!/usr/bin/env python3
"""In-container entrypoint for the runner's ``--exec docker`` mode.

The runner starts one container per benchmark cell and runs::

    python3 /bench/entry.py <harness> <model> <timeout_s>

with:
  - cwd (and the agent's write target) bind-mounted at ``/work``,
  - the adapters directory mounted read-only at ``/bench/adapters``,
  - the instruction text mounted read-only at ``/bench/instruction.txt``,
  - the harness's auth dirs mounted read-only under ``$HOME``.

This script imports the SAME adapter module the host would have used
(ADAPTER_SPEC.md, unchanged), calls ``run(instruction, "/work", model,
timeout_s)`` inside the container, and prints the returned result dict as a
single JSON line prefixed with a sentinel so the host can extract it reliably
from noisy CLI stdout::

    __BENCH_RESULT__ {"completed": true, ...}

stdlib only. Mirrors the runner's built-in ``null`` negative control so the
container round-trip can be proven without any real harness/auth.
"""

import importlib.util
import json
import os
import shutil
import sys
import traceback

try:
    from obench.auth_persist import AUTH_PERSIST
except ImportError:  # file-path / Docker mount layout
    from auth_persist import AUTH_PERSIST

RESULT_SENTINEL = "__BENCH_RESULT__"
# Container defaults; overridable via env so the entrypoint can be exercised
# on the host (and to keep the mount points configurable).
ADAPTERS_DIR = os.environ.get("BENCH_ADAPTERS_DIR", "/bench/adapters")
INSTRUCTION_PATH = os.environ.get("BENCH_INSTRUCTION_PATH", "/bench/instruction.txt")
WORKDIR = os.environ.get("BENCH_WORKDIR", "/work")
# Host auth is bind-mounted READ-ONLY here; we copy it into $HOME so CLIs that
# must write into their config home work, while the host config stays read-only.
AUTH_STAGING = os.environ.get("BENCH_AUTH_STAGING", "/bench/auth")
AUTH_RETURN = os.environ.get("BENCH_AUTH_RETURN", "/bench/auth-return")


def _stage_auth():
    """Copy read-only staged auth into the writable ``$HOME``.

    The runner mounts each host auth surface read-only under ``AUTH_STAGING``
    (e.g. ``/bench/auth/.codex``). We replicate that tree into ``$HOME`` so the
    harness sees writable credentials without ever making the host's real config
    writable. No-op when nothing is staged (e.g. the null control).
    """
    if not os.path.isdir(AUTH_STAGING):
        return
    home = os.environ.get("HOME", "/root")
    for name in os.listdir(AUTH_STAGING):
        src = os.path.join(AUTH_STAGING, name)
        dst = os.path.join(home, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copy2(src, dst)


def _return_auth(harness):
    """Atomically copy declared, possibly rotated auth into the return mount."""
    if not os.path.isdir(AUTH_RETURN):
        return
    home = os.environ.get("HOME", "/root")
    returned = set()
    for _master_relative, relative in AUTH_PERSIST.get(harness, []):
        if relative in returned:
            continue
        returned.add(relative)
        source = os.path.join(home, relative)
        if not os.path.isfile(source):
            continue
        destination = os.path.join(AUTH_RETURN, relative)
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        temporary = destination + ".tmp"
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _null_run(instruction, workdir, model, timeout_s):
    """Container-side mirror of the runner's null negative-control adapter."""
    return {
        "completed": True,
        "error": None,
        "output_tail": "",
        "tokens": None,
        "turns": None,
        "cmd": "null",
    }


def _load_adapter(name):
    path = os.path.join(ADAPTERS_DIR, f"{name}.py")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"adapter not found in container: {path}")
    spec = importlib.util.spec_from_file_location(f"bench_adapter_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise AttributeError(f"adapter '{name}' has no run() function")
    return module


def _emit(result):
    sys.stdout.flush()
    print(RESULT_SENTINEL + " " + json.dumps(result))
    sys.stdout.flush()


def main(argv):
    if len(argv) not in (4, 5):
        _emit({
            "completed": False,
            "error": f"entry.py: expected 3 or 4 args, got {len(argv) - 1}",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
        })
        return 2

    harness, model, timeout_raw = argv[1], argv[2], argv[3]
    try:
        timeout_s = int(timeout_raw)
    except ValueError:
        _emit({
            "completed": False,
            "error": f"entry.py: bad timeout {timeout_raw!r}",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
        })
        return 2

    with open(INSTRUCTION_PATH, encoding="utf-8") as fh:
        instruction = fh.read()

    # Tell adapters they are inside the disposable container, so ones that
    # normally self-sandbox (codex's bwrap needs userns, which can't nest
    # here) can rely on the container as the external sandbox instead.
    os.environ["BENCH_IN_CONTAINER"] = "1"

    try:
        _stage_auth()
        if len(argv) == 5:
            candidates_path = os.path.dirname(argv[4])
            if candidates_path not in sys.path:
                sys.path.insert(0, "/bench")
            try:
                from obench.candidates import load_candidate
            except ImportError:
                from candidates import load_candidate
            adapter = load_candidate(argv[4], ADAPTERS_DIR)
            if adapter.name != harness:
                raise ValueError(f"candidate name {adapter.name!r} does not match {harness!r}")
            try:
                candidate_version = adapter.version()
            except Exception:  # noqa: BLE001 - version failure must not fail a cell
                candidate_version = None
            result = adapter.run(instruction, WORKDIR, model, timeout_s)
            if isinstance(result, dict):
                result["candidate_version"] = candidate_version
        elif harness == "null":
            result = _null_run(instruction, WORKDIR, model, timeout_s)
        else:
            adapter = _load_adapter(harness)
            result = adapter.run(instruction, WORKDIR, model, timeout_s)
    except Exception:  # noqa: BLE001 - surface as a failed result, never crash
        result = {
            "completed": False,
            "error": "entry.py adapter exception:\n" + traceback.format_exc(limit=4).strip(),
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
        }

    persist_harness = os.environ.get("BENCH_AUTH_PERSIST_HARNESS", harness)
    try:
        _return_auth(persist_harness)
    except OSError as exc:
        result = {
            "completed": False,
            "error": f"entry.py auth persist return failed: {exc}",
            "output_tail": result.get("output_tail", ""),
            "tokens": result.get("tokens"), "turns": result.get("turns"),
            "cmd": result.get("cmd"),
        }

    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
