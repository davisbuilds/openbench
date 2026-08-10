#!/usr/bin/env python3
"""Run an autonomous full-snapshot versus auto/diff Computer Use A/B.

Both arms use the same signed computer-use-mcp binary, fixture, goal-only
prompt, model, and verifier. The only changed input is the server-locked
post-action state response mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence

import cub_v0 as cub
import scoped_agent_ab as exact

from obench.native_matrix import build_native_matrix, canonical_bytes
from obench.native_run import _canonical_digest, _content_bound_command_digest


ARMS = ("auto", "full")
TASK = "state-response-ab"
DEFAULT_EXPERIMENT_ID = "diff-full-agent-ab"
PROMPT = cub.ROOT / "experiments/diff-full-agent-ab/instruction.md"
DAEMON_BUNDLE_PATH = exact.DAEMON_BUNDLE_PATH


class ExperimentError(RuntimeError):
    pass


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
        "instruction": exact._sha256(PROMPT),
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


def _validate_arm_encodings(arm: str, counts: Mapping[str, int]) -> None:
    if arm not in ARMS:
        raise ExperimentError(f"unknown response-policy arm: {arm}")
    if counts.get("outcome", 0):
        raise ExperimentError(f"{arm} arm emitted scoped-outcome responses")
    if arm == "auto" and counts.get("diff", 0) == 0:
        raise ExperimentError("auto arm never exercised a diff response")
    if arm == "full":
        if counts.get("full", 0) == 0:
            raise ExperimentError("full arm never exercised a full response")
        unexpected = {
            encoding: count
            for encoding, count in counts.items()
            if encoding not in {"full", "none"} and count
        }
        if unexpected:
            raise ExperimentError(
                f"full arm emitted non-full state responses: {unexpected}"
            )


def _generate(
    *,
    request_path: Path,
    request: Mapping[str, Any],
    runtime_identity: Mapping[str, Any],
    source_revision: str,
    build_provenance: Mapping[str, Any],
    repetitions: int,
    experiment_id: str,
) -> tuple[dict[str, Any], Path, list[dict[str, Any]]]:
    root, _repo, _installed = cub._request_paths(request)
    config_root = cub.descendant(root, f"configs/{experiment_id}")
    config_root.mkdir(parents=True, exist_ok=True)
    host = cub._host_environment()
    fixture_identity = cub._bundle_info(root / "apps/ComputerUseFixture.app")
    mcp_identities = {
        arm: cub._mcp_plan_identity(
            runtime_identity,
            arm,
            config_root,
            state_response_mode=arm,
        )
        for arm in ARMS
    }
    spec = {
        "comparison_id": f"cub-v0-{experiment_id}",
        "task": _task_identity(request_path, request),
        "harness": {
            "name": "codex",
            "version": str(request.get("codex_version", "codex-cli 0.146.1")),
            "version_source": "native_cli",
        },
        "model": {
            "name": "gpt-5.6-sol",
            "provider": "openai-codex",
            "revision": "gpt-5.6-sol",
        },
        "arms": [
            {
                "id": arm,
                "mcp": mcp_identities[arm],
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
        arm = str(cell["arm_id"])
        trial_index = int(cell["block"])
        config_path = config_root / "cells" / TASK / f"trial{trial_index}-{arm}.toml"
        daemon_evidence = (
            cub._workspace(root, arm, TASK, trial_index) / "daemon-evidence.json"
        )
        config_text = cub._config_text(
            request_path=request_path.resolve(),
            request=request,
            arm=arm,
            task=TASK,
            trial_index=trial_index,
            trial_id=cell["trial_id"],
            mcp=runtime_identity,
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
            locked_state_response_mode=arm,
        )
        config_text += f'''\n[[artifacts]]
source = "daemon-evidence.json"
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
            "binary_sha256": runtime_identity["binary_sha256"],
            "source_revision": source_revision,
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
        "prompt_sha256": exact._sha256(PROMPT),
        "source_revision": source_revision,
        "source_archive_sha256": build_provenance["source_archive_sha256"],
        "binary_sha256": runtime_identity["binary_sha256"],
        "arms": {
            arm: {"state_response_mode": arm}
            for arm in ARMS
        },
        "fixture_profile": "small-complete-tree",
        "basic_fixture_revision": cub.BASIC_REVISION,
    }
    outputs[manifest_path] = canonical_bytes(manifest) + b"\n"
    exact._write_outputs(outputs)
    return plan, plan_path, cells


def _run_cells(
    *,
    cells: Sequence[Mapping[str, Any]],
    runtime_app: Path,
) -> list[Path]:
    bundles: list[Path] = []
    for cell in sorted(cells, key=lambda item: int(item["sequence"])):
        arm = str(cell["arm_id"])
        exact._stop_daemon()
        runtime_identity = cub._bundle_info(runtime_app)
        if runtime_identity["binary_sha256"] != cell["binary_sha256"]:
            raise ExperimentError(f"runtime binary mismatch before {cell['trial_id']}")
        _daemon_pid, daemon_identity = exact._start_exact_daemon(runtime_identity)
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
            exact._replace_trial_evidence(daemon_evidence, daemon_evidence_bytes)
            subprocess.run(
                [sys.executable, "-m", "obench", "native", "run", str(cell["config"])],
                stdin=subprocess.DEVNULL,
                check=True,
            )
            bundle = Path(str(cell["output"]))
            _validate_arm_encodings(arm, exact._response_encodings(bundle))
            sealed = bundle / DAEMON_BUNDLE_PATH
            if not sealed.is_file() or sealed.read_bytes() != daemon_evidence_bytes:
                raise ExperimentError(
                    f"sealed daemon evidence mismatch for {cell['trial_id']}"
                )
            bundles.append(bundle)
        finally:
            try:
                exact._stop_daemon()
            finally:
                Path(str(cell["daemon_evidence"])).unlink(missing_ok=True)
    return bundles


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--runtime-app", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--source-revision",
        default="748733fdf090c72d25e9a504d30e160eb34e778c",
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
    staged_app, build_provenance = exact._build_exact_app(
        root=root,
        repo=repo,
        arm="baseline",
        revision=args.source_revision,
        signing_identity=signing_identity,
    )
    if args.prepare_only:
        print(json.dumps({
            "status": "prepared",
            "build": build_provenance,
        }, indent=2, sort_keys=True))
        return 0
    if args.runtime_app is None:
        raise ExperimentError("--runtime-app is required unless --prepare-only is used")
    runtime_app = args.runtime_app.expanduser().resolve()
    cub.descendant(root, runtime_app)
    backup = runtime_app.with_name(runtime_app.name + ".diff-full-agent-ab-backup")
    if backup.exists():
        raise ExperimentError(f"stale runtime backup requires inspection: {backup}")
    if not runtime_app.is_dir():
        raise ExperimentError(f"runtime app is unavailable: {runtime_app}")
    exact._stop_daemon()
    exact._copy_bundle(runtime_app, backup)
    primary_error: BaseException | None = None
    try:
        exact._install(staged_app, runtime_app)
        runtime_identity = cub._bundle_info(runtime_app)
        staged_identity = cub._bundle_info(staged_app)
        if runtime_identity["binary_sha256"] != staged_identity["binary_sha256"]:
            raise ExperimentError("installed runtime binary does not match the pinned build")
        runtime_identity["source_revision"] = args.source_revision
        plan, plan_path, cells = _generate(
            request_path=args.request,
            request=request,
            runtime_identity=runtime_identity,
            source_revision=args.source_revision,
            build_provenance=build_provenance,
            repetitions=args.repetitions,
            experiment_id=args.experiment_id,
        )
        bundles = _run_cells(cells=cells, runtime_app=runtime_app)
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
        exact._restore_runtime_app(runtime_app, backup, primary_error=primary_error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
