#!/usr/bin/env python3
"""Admission gate for declarative BYO benchmark candidates.

Live checks spend provider credits and therefore run only with ``--live``.
The default dry run validates the manifest and prints the cells that would run.
"""

import argparse
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

try:
    import candidates
    import run as bench_run
except ImportError:  # pragma: no cover
    from . import candidates
    from . import run as bench_run

SMOKE_TASK = "make-it-run"
CORE_TASKS = ("add-feature", "build-a-cli", "fix-failing-test", "make-ci-green",
              "make-it-run", "misleading-error", "taskflow", "webcore")
IMPORTED_TASKS = ("terminal-bench/db-wal-recovery", "terminal-bench/extract-elf",
                  "terminal-bench/feal-differential-cryptanalysis",
                  "terminal-bench/gcode-to-text",
                  "terminal-bench/llm-inference-batching-scheduler",
                  "terminal-bench/raman-fitting",
                  "terminal-bench/schemelike-metacircular-eval")
INVALID_KEY = "OPENBENCH_CANDIDATE_GATE_INVALID"
NEAR_ZERO_TOKEN_LIMIT = 100


def _check(name, passed, detail, status=None):
    return {"name": name, "status": status or ("PASS" if passed else "FAIL"),
            "pass": bool(passed), "detail": detail}


def _near_zero_tokens(row):
    totals = []
    total = row.get("tokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        totals.append(total)
    fields = ("tokens_proxy_input_uncached", "tokens_proxy_cache_read",
              "tokens_proxy_cache_write", "tokens_proxy_output",
              "tokens_proxy_reasoning")
    values = [row.get(field) for field in fields
              if isinstance(row.get(field), (int, float))
              and not isinstance(row.get(field), bool)]
    if values:
        totals.append(sum(values))
    if totals:
        return max(totals) < NEAR_ZERO_TOKEN_LIMIT
    return row.get("tokens_proxy_calls") == 0


def _provider_key_names(candidate, model):
    names = set(getattr(candidate, "pass_env", []))
    names.update(name for name in getattr(candidate, "env", {})
                 if "KEY" in name.upper() or "TOKEN" in name.upper())
    if candidate.kind == "config-variant":
        for value in vars(candidate.module).values():
            if isinstance(value, dict) and model in value and isinstance(value[model], dict):
                key = value[model].get("env_key")
                if isinstance(key, str) and key:
                    names.add(key)
    return sorted(names)


def _timeout_probe(timeout_s=5):
    """Exercise the manifest process-group timeout with a deterministic stall."""
    try:
        candidates._run_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=None, timeout=timeout_s, env=dict(os.environ))
    except subprocess.TimeoutExpired:
        row = {"success": False, "completed": False,
               "error": f"timeout after {timeout_s}s", "turns": 1,
               "wall_time_s": timeout_s, "tokens_proxy_calls": 0}
        row["failure_class"] = bench_run.classify_failure(row, "", timeout_s)
        return row
    return {"failure_class": "wrong_answer"}


def _candidate_timeout_probe(candidate, model, timeout_s=5):
    """Deterministically exercise the candidate-owned timeout implementation."""
    if candidate.kind == "config-variant":
        return _timeout_probe(timeout_s)
    command, globs = candidate.command, candidate.workspace_file_globs
    candidate.command = [sys.executable, "-c", "import time; time.sleep(30)"]
    candidate.workspace_file_globs = []
    try:
        with tempfile.TemporaryDirectory(prefix="candidate_gate_timeout_") as workdir:
            result = candidate.run("timeout probe", workdir, model, timeout_s)
    finally:
        candidate.command, candidate.workspace_file_globs = command, globs
    row = dict(result)
    row.update({"success": False, "wall_time_s": timeout_s,
                "turns": row.get("turns") or 1,
                "tokens_proxy_calls": row.get("tokens_proxy_calls", 0)})
    row["failure_class"] = bench_run.classify_failure(
        row, row.get("full_output") or row.get("output_tail") or "", timeout_s)
    return row


def _policy_check(candidate):
    if candidate.kind == "config-variant":
        return True, f"inherits native {candidate.base_adapter} policy pins"
    missing = []
    for label, declared in (("headless", candidate.policy_headless_args),
                            ("auto-approve", candidate.policy_auto_approve_args)):
        if not declared:
            missing.append(f"{label} declaration")
        elif any(arg not in candidate.command for arg in declared):
            missing.append(f"{label} arg absent from command")
    return not missing, "; ".join(missing) if missing else "declared pins are present in command"


def _preview(candidate, model, timeout):
    if candidate.kind == "manifest":
        values = {"prompt": "<task instruction>", "workspace": "<workspace>",
                  "model": candidate.models.get(model, model), "home": "<isolated HOME>"}
        command = [candidates._expand(part, values) for part in candidate.command]
    else:
        command = [f"native adapter {candidate.base_adapter}", "with staged candidate config"]
    return {"smoke": command, "timeout_s": 5, "failure_honesty_keys": list(
        getattr(candidate, "pass_env", [])), "calibration_tasks": list(CORE_TASKS + IMPORTED_TASKS)}


def gate(spec_path, model, *, live=False, calibrate=False, timeout=2400,
         adapters_dir=None, tasks_dir=None, imported_tasks_dir=None,
         cell_runner=None, timeout_runner=None, proxy_ctx=None):
    adapters_dir = adapters_dir or os.path.join(os.path.dirname(__file__), "adapters")
    tasks_dir = tasks_dir or bench_run.DEFAULT_TASKS_DIR
    imported_tasks_dir = imported_tasks_dir or os.path.join(bench_run.REPO, "tasks-imported")
    candidate = candidates.load_candidate(spec_path, adapters_dir)
    checks = []
    policy_ok, policy_detail = _policy_check(candidate)
    checks.append(_check("POLICY", policy_ok, policy_detail))

    isolated = candidate.kind == "config-variant" or candidate.isolate_home
    checks.append(_check("ISOLATION", isolated,
                         "isolated HOME enabled; canary transcript/workspace heuristic"
                         if isolated else "manifest must set isolate_home=true"))
    version_declared = (candidate.kind == "config-variant" or bool(candidate.version_command))
    version = candidate.version() if live else None
    checks.append(_check("VERSION", bool(version) if live else version_declared,
                         (f"version_command returned {version!r}" if version else
                          "version probe declared (WOULD run)" if version_declared and not live else
                          "version command returned empty")))

    preview = _preview(candidate, model, timeout)
    smoke_row = None
    if not live:
        proxy_declared = (candidate.kind == "config-variant" or candidate.unmetered or
                          bool(candidate.base_url_env and candidate.proxy_route))
        checks.append(_check("METERING", proxy_declared,
                             "WOULD run proxy smoke cell" if proxy_declared else
                             "declare proxy routing or unmetered=true"))
        checks.append(_check("FAILURE HONESTY", True,
                             "WOULD run with provider key env unset/bogus"))
        checks.append(_check("CALIBRATION", True,
                             "WOULD run n=1 across 15 tasks" if calibrate else
                             "off (pass --calibrate --live)"))
    else:
        if cell_runner is None:
            cell_runner = bench_run.run_cell
        def run_cell(task, cap, root=tasks_dir, **extra):
            return cell_runner(candidate.name, task, model, 1, cap, root, adapters_dir, 120,
                               harness_version=version, proxy_ctx=proxy_ctx,
                               candidate=candidate, **extra)

        sentinel = "OPENBENCH_HOME_CANARY_7c51b9"
        workspace_canary_seen = [False]
        def observe_workspace(workdir):
            needle = sentinel.encode()
            remaining = 16 * 1024 * 1024
            for root, _dirs, files in os.walk(workdir, followlinks=False):
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        mode = os.lstat(path).st_mode
                        if not stat.S_ISREG(mode) or os.path.islink(path):
                            continue
                        with open(path, "rb") as fh:
                            tail = b""
                            while remaining > 0:
                                chunk = fh.read(min(64 * 1024, remaining))
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                                if needle in tail + chunk:
                                    workspace_canary_seen[0] = True
                                    return
                                tail = chunk[-(len(needle) - 1):]
                    except OSError:
                        pass
                    if remaining <= 0:
                        return

        with tempfile.TemporaryDirectory(prefix="candidate_gate_evidence_") as evidence_dir:
            with tempfile.TemporaryDirectory(prefix="candidate_gate_home_") as fake_home:
                with open(os.path.join(fake_home, ".openbench-canary"), "w", encoding="utf-8") as fh:
                    fh.write(sentinel)
                # Resolve and mirror declared auth sources before HOME becomes
                # the canary root; ManifestHarness will then stage the same
                # credentials into its separate child HOME.
                for auth in getattr(candidate, "auth_files", []):
                    source = candidates._auth_source(auth["source"])
                    mirrored = os.path.join(fake_home, auth["source"][2:])
                    os.makedirs(os.path.dirname(mirrored), exist_ok=True)
                    shutil.copy2(source, mirrored)
                with contextlib.ExitStack() as stack:
                    old_home = os.environ.get("HOME")
                    os.environ["HOME"] = fake_home
                    stack.callback(lambda: os.environ.__setitem__("HOME", old_home)
                                   if old_home is not None else os.environ.pop("HOME", None))
                    smoke_row = run_cell(
                        SMOKE_TASK, timeout, transcripts_dir=evidence_dir,
                        results_stem="candidate-gate", workspace_observer=observe_workspace)
            evidence = json.dumps(smoke_row, sort_keys=True)
            for root, _dirs, files in os.walk(evidence_dir):
                for name in files:
                    try:
                        with open(os.path.join(root, name), encoding="utf-8",
                                  errors="replace") as fh:
                            evidence += fh.read()
                    except OSError:
                        pass
            canary_seen = sentinel in evidence or workspace_canary_seen[0]
        checks[1] = _check("ISOLATION", isolated and not canary_seen,
                           "canary absent from row transcript/workspace evidence"
                           if not canary_seen else "canary content escaped into row evidence")
        calls = smoke_row.get("tokens_proxy_calls") or 0
        smoke_worked = bool(smoke_row.get("completed")) and not smoke_row.get("error")
        metered = calls > 0 or (candidate.unmetered and smoke_worked)
        checks.append(_check(
            "METERING", metered,
            (f"manifest declares unmetered=true; smoke_completed={smoke_worked}"
             if candidate.unmetered else f"counting proxy ledger calls={calls}")))

        # Replace only the executable for this probe, retaining the candidate's
        # own run/timeout implementation while avoiding model-dependent behavior.
        timeout_row = (timeout_runner or
                       (lambda seconds: _candidate_timeout_probe(candidate, model, seconds)))(5)
        timeout_ok = timeout_row.get("failure_class") == "timeout"
        checks[0] = _check(
            "POLICY", policy_ok and timeout_ok,
            policy_detail + f"; deterministic 5s timeout classified "
            f"{timeout_row.get('failure_class')!r}")

        key_names = _provider_key_names(candidate, model)
        saved = {name: os.environ.get(name) for name in key_names}
        saved_candidate_env = dict(getattr(candidate, "env", {}))
        saved_auth = list(getattr(candidate, "auth_files", []))
        unsafe_inheritance = bool(getattr(candidate, "inherit_env", False))
        try:
            for name in key_names:
                os.environ[name] = INVALID_KEY
                if name in candidate.env:
                    candidate.env[name] = INVALID_KEY
            # Do not stage a real auth file for the deliberate bad-credential cell.
            candidate.auth_files = []
            with tempfile.TemporaryDirectory(prefix="candidate_gate_bad_auth_home_") as bad_home:
                old_home = os.environ.get("HOME")
                os.environ["HOME"] = bad_home
                try:
                    honesty_row = run_cell(SMOKE_TASK, timeout)
                finally:
                    if old_home is None:
                        os.environ.pop("HOME", None)
                    else:
                        os.environ["HOME"] = old_home
        finally:
            candidate.auth_files = saved_auth
            candidate.env.clear()
            candidate.env.update(saved_candidate_env)
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        credential_invalidated = bool(key_names or saved_auth)
        honesty_ok = (not unsafe_inheritance and credential_invalidated
                      and honesty_row.get("failure_class") in {"infra", "rate_limited"}
                      and _near_zero_tokens(honesty_row))
        detail = ("inherit_env=true prevents safe credential invalidation"
                  if unsafe_inheritance else
                  f"key_env={key_names}; failure_class={honesty_row.get('failure_class')!r}; "
                  f"near_zero_tokens={_near_zero_tokens(honesty_row)}")
        checks.append(_check("FAILURE HONESTY", honesty_ok, detail))

        if calibrate:
            rows = [run_cell(task, timeout) for task in CORE_TASKS]
            rows += [run_cell(task, timeout, imported_tasks_dir) for task in IMPORTED_TASKS]
            solves = sum(bool(row.get("success")) for row in rows)
            anomaly = solves in {0, len(rows)}
            checks.append(_check("CALIBRATION", not anomaly,
                                 f"solves={solves}/{len(rows)}" + ("; anomaly: all-zero/all-perfect" if anomaly else "")))
        else:
            checks.append(_check("CALIBRATION", True, "off (pass --calibrate)"))

    if live and smoke_row is not None:
        stamped = smoke_row.get("harness_version")
        version_ok = bool(version and stamped == version)
        checks[2] = _check("VERSION", version_ok,
                           f"version={version!r}; smoke stamp={stamped!r}")

    failed = [item for item in checks if item["status"] == "FAIL"]
    return {"candidate": candidate.name, "candidate_path": os.path.abspath(spec_path),
            "model": model, "mode": "live" if live else "dry-run", "pass": not failed,
            "status": "PASS" if not failed else "FAIL", "version": version,
            "smoke_row": smoke_row, "preview": preview, "checks": checks}


def _proxy_context():
    import proxy
    ledger = tempfile.mkdtemp(prefix="candidate_gate_proxy_")
    server, _thread = proxy.start_in_thread("127.0.0.1", 0, ledger)
    port = server.server_address[1]
    return server, ledger, {"ledger_dir": ledger,
                            "local_base_url": f"http://127.0.0.1:{port}",
                            "docker_base_url": f"http://host.docker.internal:{port}"}


def print_result(result):
    for item in result["checks"]:
        print(f"{item['name']}: {item['status']} - {item['detail']}")
    if result["mode"] == "dry-run":
        print("WOULD RUN:", json.dumps(result["preview"], sort_keys=True))
    print(f"VERDICT: {result['status']} ({result['mode']})")
    print("JSON:", json.dumps(result, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate")
    parser.add_argument("--model", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--timeout", type=int, default=2400)
    args = parser.parse_args(argv)
    if args.calibrate and not args.live:
        pass  # preview remains useful and spends nothing
    server = ledger = proxy_ctx = None
    try:
        if args.live:
            server, ledger, proxy_ctx = _proxy_context()
        result = gate(args.candidate, args.model, live=args.live,
                      calibrate=args.calibrate, timeout=args.timeout, proxy_ctx=proxy_ctx)
        print_result(result)
        return 0 if result["pass"] else 3
    except (OSError, ValueError, KeyError) as exc:
        print(f"SCHEMA: FAIL - {exc}")
        record = {"candidate_path": os.path.abspath(args.candidate), "model": args.model,
                  "mode": "live" if args.live else "dry-run", "status": "FAIL",
                  "pass": False, "error": str(exc)}
        print("VERDICT: FAIL")
        print("JSON:", json.dumps(record, sort_keys=True))
        return 3
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if ledger:
            shutil.rmtree(ledger, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
