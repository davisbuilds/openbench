#!/usr/bin/env python3
"""Run the scoped-outcome experiment through a real Codex agent.

Both arms use one prompt and the canonical native runner. The only changed
input is the exact signed computer-use-mcp binary installed at the already
authorized runtime path for that matrix cell.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

import cub_v0 as cub

from obench.native_matrix import build_native_matrix, canonical_bytes
from obench.native_run import _canonical_digest, _content_bound_command_digest


ARMS = ("baseline", "scoped")
DEFAULT_EXPERIMENT_ID = "scoped-agent-ab"
TASK = "basic-controls"
PROMPT = cub.ROOT / "experiments/scoped-outcome-agent-ab/instruction.md"


class ExperimentError(RuntimeError):
    pass


DAEMON_RUNTIME = Path.home() / "Library/Caches/computer-use-mcp"
DAEMON_SOCKET = DAEMON_RUNTIME / "daemon.sock"
DAEMON_LOCK = DAEMON_RUNTIME / "daemon.lock"
DAEMON_SECRET = DAEMON_RUNTIME / "daemon.secret"
DAEMON_TIMEOUT_SECONDS = 10.0
DAEMON_BUNDLE_PATH = "artifacts/final-state/daemon-evidence.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_stable_bundle(app: Path) -> dict[str, Any]:
    identity = cub._bundle_info(app)
    requirement = str(identity.get("designated_requirement", ""))
    if identity.get("adhoc") or requirement.startswith("cdhash "):
        raise ExperimentError(
            f"app bundle lacks a stable code-signing requirement: {app}"
        )
    if identity.get("bundle_id") != cub.SOURCE_MCP_BUNDLE_ID:
        raise ExperimentError(f"app bundle has an unexpected bundle identifier: {app}")
    return identity


def _install(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    subprocess.run(
        ["ditto", str(source), str(destination)],
        stdin=subprocess.DEVNULL,
        check=True,
        timeout=60,
    )


def _daemon_lock_owners() -> list[int]:
    lsof = shutil.which("lsof")
    if lsof is None:
        raise ExperimentError("lsof is required to prove daemon process ownership")
    completed = subprocess.run(
        [lsof, "-t", "--", str(DAEMON_LOCK)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode not in (0, 1):
        raise ExperimentError(f"cannot inspect daemon lock owner: {completed.stderr.strip()}")
    try:
        return sorted({int(line) for line in completed.stdout.splitlines() if line.strip()})
    except ValueError as exc:
        raise ExperimentError("lsof returned a non-numeric daemon lock owner") from exc


def _daemon_secret() -> str:
    if not DAEMON_SECRET.is_file() or DAEMON_SECRET.is_symlink():
        raise ExperimentError("daemon authentication secret is unavailable")
    mode = DAEMON_SECRET.stat().st_mode & 0o777
    if mode & 0o077:
        raise ExperimentError("daemon authentication secret permissions are too broad")
    secret = DAEMON_SECRET.read_text(encoding="utf-8").strip()
    if not secret:
        raise ExperimentError("daemon authentication secret is empty")
    return secret


def _daemon_exchange(requests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(DAEMON_TIMEOUT_SECONDS)
    try:
        client.connect(str(DAEMON_SOCKET))
        buffer = b""
        for request in requests:
            client.sendall(canonical_bytes(request) + b"\n")
            while b"\n" not in buffer:
                chunk = client.recv(65536)
                if not chunk:
                    raise ExperimentError("daemon closed before returning a response")
                buffer += chunk
            line, buffer = buffer.split(b"\n", 1)
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExperimentError("daemon returned malformed JSON") from exc
            if not isinstance(response, dict) or response.get("id") != request.get("id"):
                raise ExperimentError("daemon returned a mismatched response")
            responses.append(response)
    except OSError as exc:
        raise ExperimentError(f"cannot communicate with engine daemon: {exc}") from exc
    finally:
        client.close()
    return responses


def _daemon_hello() -> dict[str, Any]:
    secret = _daemon_secret()
    response = _daemon_exchange([{
        "id": 1,
        "method": "hello",
        "version": cub.MCP_VERSION,
        "authToken": secret,
        "buildStamp": 0,
    }])[0]
    if response.get("isError") is True or response.get("authenticated") is not True:
        raise ExperimentError("daemon handshake was not authenticated")
    if not isinstance(response.get("daemonIncarnationID"), str):
        raise ExperimentError("daemon handshake omitted its incarnation identity")
    if not isinstance(response.get("buildStamp"), (int, float)):
        raise ExperimentError("daemon handshake omitted its executable build stamp")
    return response


def _stop_daemon() -> dict[str, Any] | None:
    owners = _daemon_lock_owners()
    if not owners:
        if DAEMON_SOCKET.exists():
            if DAEMON_SOCKET.is_symlink() or not stat.S_ISSOCK(DAEMON_SOCKET.stat().st_mode):
                raise ExperimentError("unowned daemon socket path is not a Unix socket")
            DAEMON_SOCKET.unlink()
        return None
    if len(owners) != 1 or not DAEMON_SOCKET.exists():
        raise ExperimentError(
            f"daemon runtime is inconsistent: lock_owners={owners}, socket={DAEMON_SOCKET.exists()}"
        )
    secret = _daemon_secret()
    hello, shutdown = _daemon_exchange([
        {
            "id": 1,
            "method": "hello",
            "version": cub.MCP_VERSION,
            "authToken": secret,
            "buildStamp": 0,
        },
        {
            "id": 2,
            "method": "shutdown",
            "authToken": secret,
            "buildStamp": float("1.7976931348623157e308"),
        },
    ])
    if hello.get("authenticated") is not True or shutdown.get("isError") is True:
        raise ExperimentError("daemon refused the benchmark-owned graceful shutdown")
    deadline = time.monotonic() + DAEMON_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _daemon_lock_owners() and not DAEMON_SOCKET.exists():
            return {
                "pid": owners[0],
                "incarnation_id": hello.get("daemonIncarnationID"),
                "version": hello.get("version"),
                "build_stamp": hello.get("buildStamp"),
            }
        time.sleep(0.05)
    raise ExperimentError("engine daemon did not exit after graceful shutdown")


def _validate_daemon_identity(
    observed: Mapping[str, Any], *, executable: Path, binary_sha256: str, pid: int
) -> dict[str, Any]:
    owners = _daemon_lock_owners()
    if owners != [pid]:
        raise ExperimentError(f"spawned daemon does not exclusively own its lock: {owners}")
    expected_stamp = executable.stat().st_mtime
    observed_stamp = observed.get("buildStamp")
    if (
        observed.get("version") != cub.MCP_VERSION
        or observed.get("authenticated") is not True
        or not isinstance(observed_stamp, (int, float))
        or not math.isclose(float(observed_stamp), expected_stamp, abs_tol=0.001)
        or _sha256(executable) != binary_sha256
    ):
        raise ExperimentError("daemon identity does not match the installed experiment arm")
    return {
        "pid": pid,
        "incarnation_id": observed["daemonIncarnationID"],
        "version": observed["version"],
        "build_stamp": observed_stamp,
        "executable": str(executable),
        "binary_sha256": binary_sha256,
    }


def _start_exact_daemon(identity: Mapping[str, Any]) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    if _daemon_lock_owners() or DAEMON_SOCKET.exists():
        raise ExperimentError("cannot start an exact daemon while another daemon is present")
    executable = Path(str(identity["executable"]))
    process = subprocess.Popen(
        [str(executable), "daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + DAEMON_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ExperimentError("exact daemon exited during startup")
            if DAEMON_SOCKET.exists():
                observed = _daemon_hello()
                return process, _validate_daemon_identity(
                    observed,
                    executable=executable,
                    binary_sha256=str(identity["binary_sha256"]),
                    pid=process.pid,
                )
            time.sleep(0.05)
    except Exception:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise
    process.terminate()
    process.wait(timeout=5)
    raise ExperimentError("exact daemon did not become ready")


def _response_encodings(bundle: Path) -> dict[str, int]:
    ledger = bundle / "mcp/ledger.jsonl"
    counts: dict[str, int] = {}
    for raw in ledger.read_text(encoding="utf-8").splitlines():
        row = json.loads(raw)
        if row.get("record_type") != "tool_call":
            continue
        perception = (
            row.get("computer_use_meta", {}).get("metrics") or {}
        ).get("perception")
        if isinstance(perception, dict) and isinstance(perception.get("response_encoding"), str):
            encoding = perception["response_encoding"]
            counts[encoding] = counts.get(encoding, 0) + 1
    return dict(sorted(counts.items()))


def _validate_arm_encodings(arm: str, counts: Mapping[str, int]) -> None:
    outcomes = counts.get("outcome", 0)
    if arm == "baseline" and outcomes:
        raise ExperimentError("baseline emitted scoped-only outcome responses")
    if arm == "scoped" and outcomes == 0:
        raise ExperimentError("scoped arm never exercised its outcome response")


def _archive_sha256(repo: Path, revision: str) -> str:
    completed = subprocess.run(
        ["git", "archive", "--format=tar", revision],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        raise ExperimentError(f"cannot archive source commit {revision}: {detail}")
    return hashlib.sha256(completed.stdout).hexdigest()


def _build_exact_app(
    *,
    root: Path,
    repo: Path,
    arm: str,
    revision: str,
    signing_identity: str,
) -> tuple[Path, dict[str, Any]]:
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if resolved.returncode != 0:
        raise ExperimentError(f"cannot resolve {arm} source revision {revision}")
    source_revision = resolved.stdout.strip()
    if source_revision != revision or len(source_revision) != 40:
        raise ExperimentError(f"{arm} revision must be one exact full Git commit SHA")

    build_root = cub.descendant(root, f"experiment-builds/scoped-agent-ab/{source_revision}")
    source_tree = build_root / "source"
    apps = build_root / "apps"
    app = apps / "OpenBench Computer Use MCP Source.app"
    provenance_path = build_root / "provenance.json"
    archive_sha256 = _archive_sha256(repo, source_revision)

    if provenance_path.is_file():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if not app.is_dir():
            raise ExperimentError(f"{arm} cached build is missing its app bundle")
        identity = _require_stable_bundle(app)
        expected = {
            "arm": arm,
            "source_revision": source_revision,
            "source_archive_sha256": archive_sha256,
            "binary_sha256": identity["binary_sha256"],
            "bundle_id": cub.SOURCE_MCP_BUNDLE_ID,
            "designated_requirement": identity["designated_requirement"],
        }
        if any(provenance.get(key) != value for key, value in expected.items()):
            raise ExperimentError(f"{arm} cached build provenance does not match")
        return app, provenance

    cub._extract_revision(repo, source_revision, source_tree)
    apps.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            sys.executable,
            str(source_tree / "scripts/build_app_bundle.py"),
            "--app-name",
            "OpenBench Computer Use MCP Source",
            "--bundle-id",
            cub.SOURCE_MCP_BUNDLE_ID,
            "--configuration",
            "release",
            "--identity",
            signing_identity,
            "--install",
            str(apps),
        ],
        cwd=source_tree,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ExperimentError(f"failed to build {arm} revision {source_revision}: {detail}")
    try:
        build_result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ExperimentError(f"{arm} build did not return JSON provenance") from exc
    if not build_result.get("signed") or build_result.get("signing_error"):
        detail = str(build_result.get("signing_error") or "signing did not succeed")
        raise ExperimentError(f"failed to sign {arm} revision: {detail}")
    if Path(str(build_result.get("installed_bundle"))).resolve() != app.resolve():
        raise ExperimentError(f"{arm} build installed an unexpected app bundle")

    identity = _require_stable_bundle(app)
    provenance = {
        "schema_version": "openbench.computer-use-exact-build.v1",
        "arm": arm,
        "source_revision": source_revision,
        "source_archive_sha256": archive_sha256,
        "binary_sha256": identity["binary_sha256"],
        "bundle_id": identity["bundle_id"],
        "designated_requirement": identity["designated_requirement"],
    }
    cub._write_immutable_outputs(
        {provenance_path: canonical_bytes(provenance) + b"\n"}
    )
    return app, provenance


def _task_identity(request_path: Path, request: Mapping[str, Any]) -> dict[str, Any]:
    root, _repo, _installed = cub._request_paths(request)
    verifier_command = [
        sys.executable,
        str(Path(cub.__file__).resolve()),
        "--request",
        str(request_path.resolve()),
        "verify",
    ]
    verifier_digest = _content_bound_command_digest(
        verifier_command,
        cwd=cub._workspace(root, ARMS[0], TASK, 1),
        extra_paths=cub._oracle_paths(TASK),
    )
    task_content = {
        "instruction": _sha256(PROMPT),
        "verifier": verifier_digest,
        "artifacts": [
            "artifacts/final-state/state.json",
            DAEMON_BUNDLE_PATH,
        ],
    }
    return {
        "name": f"openbench/computer-use-v0-{TASK}",
        "content_sha256": _canonical_digest(task_content),
    }


def _write_outputs(outputs: Mapping[Path, bytes]) -> None:
    cub._write_immutable_outputs(dict(outputs))


def _generate(
    *,
    request_path: Path,
    request: Mapping[str, Any],
    staged_apps: Mapping[str, Path],
    revisions: Mapping[str, str],
    runtime_app: Path,
    repetitions: int,
    experiment_id: str,
    build_provenance: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], Path, Path, list[dict[str, Any]]]:
    root, _repo, _installed = cub._request_paths(request)
    config_root = cub.descendant(root, f"configs/{experiment_id}")
    config_root.mkdir(parents=True, exist_ok=True)
    runtime_identities: dict[str, dict[str, Any]] = {}
    mcp_plan_identities: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        _install(staged_apps[arm], runtime_app)
        identity = cub._bundle_info(runtime_app)
        staged_identity = cub._bundle_info(staged_apps[arm])
        if identity["binary_sha256"] != staged_identity["binary_sha256"]:
            raise ExperimentError(f"installed {arm} binary does not match its staged app")
        identity["source_revision"] = revisions[arm]
        runtime_identities[arm] = identity
        mcp_plan_identities[arm] = cub._mcp_plan_identity(
            identity, arm, config_root, state_response_mode="auto"
        )

    host = cub._host_environment()
    fixture_identity = cub._bundle_info(root / "apps/ComputerUseFixture.app")
    harness = {
        "name": "codex",
        "version": str(request.get("codex_version", "codex-cli 0.146.1")),
        "version_source": "native_cli",
    }
    model = {
        "name": "gpt-5.6-sol",
        "provider": "openai-codex",
        "revision": "gpt-5.6-sol",
    }
    spec = {
        "comparison_id": f"cub-v0-{experiment_id}",
        "task": _task_identity(request_path, request),
        "harness": harness,
        "model": model,
        "arms": [
            {
                "id": arm,
                "mcp": mcp_plan_identities[arm],
                "config": cub._arm_plan_config(fixture_identity, host),
            }
            for arm in ARMS
        ],
        "repetitions": repetitions,
    }
    plan = build_native_matrix(**spec)
    plan_dir = config_root / "plans"
    spec_path = plan_dir / f"{TASK}.spec.json"
    plan_path = plan_dir / f"{TASK}.plan.json"
    manifest_path = config_root / "manifest.json"
    outputs: dict[Path, bytes] = {
        spec_path: canonical_bytes(spec) + b"\n",
        plan_path: canonical_bytes(plan) + b"\n",
    }
    cells: list[dict[str, Any]] = []
    for cell in plan["schedule"]:
        arm = cell["arm_id"]
        trial_index = int(cell["block"])
        config_path = (
            config_root / "cells" / TASK / f"trial{trial_index}-{arm}.toml"
        )
        daemon_evidence = config_path.with_name(
            config_path.stem + ".daemon.json"
        )
        config_text = cub._config_text(
            request_path=request_path.resolve(),
            request=request,
            arm=arm,
            task=TASK,
            trial_index=trial_index,
            trial_id=cell["trial_id"],
            mcp=runtime_identities[arm],
            app=fixture_identity,
            host=host,
            mode=experiment_id,
            matrix={
                **cell,
                "plan_sha256": plan["plan_sha256"],
                "manifest": manifest_path,
                "plan": plan_path,
            },
            instruction_path=PROMPT,
            locked_state_response_mode="auto",
        )
        config_text += f'''\n[[artifacts]]
source = {cub._toml_string(str(daemon_evidence))}
path = {cub._toml_string(DAEMON_BUNDLE_PATH)}
media_type = "application/json"
'''
        config_bytes = config_text.encode("utf-8")
        outputs[config_path] = config_bytes
        output_path, results_path = cub._result_paths(
            root, experiment_id, arm, TASK, trial_index
        )
        cells.append({
            "task": TASK,
            **{
                key: cell[key]
                for key in (
                    "sequence", "block", "position", "arm_id", "cell_id",
                    "trial_id", "config_sha256", "cell_sha256",
                )
            },
            "trial_index": trial_index,
            "matrix_cell_key": f"{TASK}/{cell['cell_id']}",
            "plan_sha256": plan["plan_sha256"],
            "config": str(config_path),
            "runnable_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "output": str(output_path),
            "results": str(results_path),
            "daemon_evidence": str(daemon_evidence),
            "binary_sha256": runtime_identities[arm]["binary_sha256"],
            "source_revision": revisions[arm],
        })
    manifest = {
        "schema_version": "openbench.computer-use-config-set.v2",
        "mode": experiment_id,
        "comparable": True,
        "repetitions": repetitions,
        "plans": [{
            "task": TASK,
            "spec": str(spec_path),
            "spec_sha256": hashlib.sha256(outputs[spec_path]).hexdigest(),
            "plan": str(plan_path),
            "plan_sha256": plan["plan_sha256"],
            "plan_file_sha256": hashlib.sha256(outputs[plan_path]).hexdigest(),
        }],
        "cells": cells,
        "prompt": str(PROMPT),
        "prompt_sha256": _sha256(PROMPT),
        "arms": {
            arm: {
                "source_revision": revisions[arm],
                "source_archive_sha256": build_provenance[arm][
                    "source_archive_sha256"
                ],
                "binary_sha256": runtime_identities[arm]["binary_sha256"],
            }
            for arm in ARMS
        },
        "basic_fixture_revision": cub.BASIC_REVISION,
    }
    outputs[manifest_path] = canonical_bytes(manifest) + b"\n"
    _write_outputs(outputs)
    return plan, plan_path, manifest_path, cells


def _run_cells(
    *,
    cells: Sequence[Mapping[str, Any]],
    staged_apps: Mapping[str, Path],
    runtime_app: Path,
) -> list[Path]:
    bundles: list[Path] = []
    for cell in sorted(cells, key=lambda item: int(item["sequence"])):
        arm = str(cell["arm_id"])
        _stop_daemon()
        _install(staged_apps[arm], runtime_app)
        runtime_identity = cub._bundle_info(runtime_app)
        if runtime_identity["binary_sha256"] != cell["binary_sha256"]:
            raise ExperimentError(f"runtime binary mismatch before {cell['trial_id']}")
        daemon_process, daemon_identity = _start_exact_daemon(runtime_identity)
        print(f"RUN {cell['sequence']}: {arm} block={cell['block']}", flush=True)
        try:
            daemon_evidence = Path(str(cell["daemon_evidence"]))
            daemon_evidence_bytes = canonical_bytes({
                "schema_version": "openbench.computer-use-daemon-evidence.v1",
                "trial_id": cell["trial_id"],
                "arm": arm,
                "source_revision": cell["source_revision"],
                "daemon": daemon_identity,
            }) + b"\n"
            _write_outputs({daemon_evidence: daemon_evidence_bytes})
            subprocess.run(
                [sys.executable, "-m", "obench", "native", "run", str(cell["config"])],
                stdin=subprocess.DEVNULL,
                check=True,
            )
            bundle = Path(str(cell["output"]))
            encodings = _response_encodings(bundle)
            _validate_arm_encodings(arm, encodings)
            sealed_daemon_evidence = bundle / DAEMON_BUNDLE_PATH
            if (
                not sealed_daemon_evidence.is_file()
                or sealed_daemon_evidence.read_bytes() != daemon_evidence_bytes
            ):
                raise ExperimentError(
                    f"sealed daemon evidence mismatch for {cell['trial_id']}"
                )
            bundles.append(bundle)
        finally:
            _stop_daemon()
            daemon_process.wait(timeout=DAEMON_TIMEOUT_SECONDS)
    return bundles


def _restore_runtime_app(
    runtime_app: Path,
    backup: Path,
    *,
    primary_error: BaseException | None,
) -> None:
    cleanup_error: BaseException | None = None
    try:
        _stop_daemon()
    except BaseException as exc:
        cleanup_error = exc
    try:
        if runtime_app.exists():
            shutil.rmtree(runtime_app)
        os.replace(backup, runtime_app)
    except BaseException as restore_error:
        if cleanup_error is not None:
            restore_error.add_note(f"daemon cleanup also failed: {cleanup_error}")
        if primary_error is not None:
            primary_error.add_note(f"runtime app restoration failed: {restore_error}")
            return
        raise
    if cleanup_error is not None:
        if primary_error is not None:
            primary_error.add_note(f"daemon cleanup failed: {cleanup_error}")
            return
        raise cleanup_error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--runtime-app", type=Path)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="build and verify both exact signed revisions without running trials",
    )
    parser.add_argument(
        "--baseline-revision",
        default="748733fdf090c72d25e9a504d30e160eb34e778c",
    )
    parser.add_argument(
        "--scoped-revision",
        default="097a704b87c27d0bb4182a4e2855d891483fb769",
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--experiment-id", default=DEFAULT_EXPERIMENT_ID)
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        raise ExperimentError("repetitions must be positive")
    if not args.experiment_id or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in args.experiment_id
    ):
        raise ExperimentError(
            "experiment-id must contain only lowercase letters, numbers, and hyphens"
        )
    request = cub._load_request(args.request)
    root, repo, _installed = cub._request_paths(request)
    signing_identity = request.get("source_signing_identity")
    if not isinstance(signing_identity, str) or not signing_identity or signing_identity == "-":
        raise ExperimentError("source_signing_identity must be a stable signing identity")
    revisions = {
        "baseline": args.baseline_revision,
        "scoped": args.scoped_revision,
    }
    staged_apps: dict[str, Path] = {}
    build_provenance: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        app, provenance = _build_exact_app(
            root=root,
            repo=repo,
            arm=arm,
            revision=revisions[arm],
            signing_identity=signing_identity,
        )
        staged_apps[arm] = app
        build_provenance[arm] = provenance
    if args.prepare_only:
        print(json.dumps({
            "status": "prepared",
            "builds": build_provenance,
        }, indent=2, sort_keys=True))
        return 0
    if args.runtime_app is None:
        raise ExperimentError("--runtime-app is required unless --prepare-only is used")
    runtime_app = args.runtime_app.expanduser().resolve()
    cub.descendant(root, runtime_app)
    backup = runtime_app.with_name(runtime_app.name + ".scoped-agent-ab-backup")
    if backup.exists():
        raise ExperimentError(f"stale runtime backup requires inspection: {backup}")
    if not runtime_app.is_dir():
        raise ExperimentError(f"runtime app is unavailable: {runtime_app}")
    _stop_daemon()
    os.replace(runtime_app, backup)
    primary_error: BaseException | None = None
    try:
        plan, plan_path, _manifest_path, cells = _generate(
            request_path=args.request,
            request=request,
            staged_apps=staged_apps,
            revisions=revisions,
            runtime_app=runtime_app,
            repetitions=args.repetitions,
            experiment_id=args.experiment_id,
            build_provenance=build_provenance,
        )
        bundles = _run_cells(
            cells=cells,
            staged_apps=staged_apps,
            runtime_app=runtime_app,
        )
        report = cub.descendant(root, f"results/{args.experiment_id}/report.json")
        command = [
            sys.executable, "-m", "obench", "native", "report",
            str(plan_path), "--output", str(report),
        ]
        for bundle in bundles:
            command.extend(("--bundle", str(bundle)))
        subprocess.run(command, stdin=subprocess.DEVNULL, check=True)
        print(json.dumps({
            "status": "passed",
            "plan_sha256": plan["plan_sha256"],
            "report": str(report),
            "bundles": [str(path) for path in bundles],
        }, indent=2, sort_keys=True))
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _restore_runtime_app(runtime_app, backup, primary_error=primary_error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
