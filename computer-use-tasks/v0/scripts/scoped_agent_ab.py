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
import os
from pathlib import Path
import shutil
import subprocess
import sys
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _install(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    subprocess.run(
        ["ditto", str(source), str(destination)],
        stdin=subprocess.DEVNULL,
        check=True,
        timeout=60,
    )


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
        "artifacts": ["artifacts/final-state/state.json"],
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
            identity, arm, config_root
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
        config_bytes = cub._config_text(
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
        ).encode("utf-8")
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
        _install(staged_apps[arm], runtime_app)
        observed = cub._bundle_info(runtime_app)["binary_sha256"]
        if observed != cell["binary_sha256"]:
            raise ExperimentError(f"runtime binary mismatch before {cell['trial_id']}")
        print(f"RUN {cell['sequence']}: {arm} block={cell['block']}", flush=True)
        subprocess.run(
            [sys.executable, "-m", "obench", "native", "run", str(cell["config"])],
            stdin=subprocess.DEVNULL,
            check=True,
        )
        bundles.append(Path(str(cell["output"])))
    return bundles


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--baseline-app", required=True, type=Path)
    parser.add_argument("--scoped-app", required=True, type=Path)
    parser.add_argument("--runtime-app", required=True, type=Path)
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
    root, _repo, _installed = cub._request_paths(request)
    runtime_app = args.runtime_app.expanduser().resolve()
    cub.descendant(root, runtime_app)
    staged_apps = {
        "baseline": args.baseline_app.expanduser().resolve(),
        "scoped": args.scoped_app.expanduser().resolve(),
    }
    for arm, app in staged_apps.items():
        if not app.is_dir():
            raise ExperimentError(f"{arm} app is unavailable: {app}")
    backup = runtime_app.with_name(runtime_app.name + ".scoped-agent-ab-backup")
    if backup.exists():
        raise ExperimentError(f"stale runtime backup requires inspection: {backup}")
    if not runtime_app.is_dir():
        raise ExperimentError(f"runtime app is unavailable: {runtime_app}")
    os.replace(runtime_app, backup)
    try:
        plan, plan_path, _manifest_path, cells = _generate(
            request_path=args.request,
            request=request,
            staged_apps=staged_apps,
            revisions={
                "baseline": args.baseline_revision,
                "scoped": args.scoped_revision,
            },
            runtime_app=runtime_app,
            repetitions=args.repetitions,
            experiment_id=args.experiment_id,
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
    finally:
        if runtime_app.exists():
            shutil.rmtree(runtime_app)
        os.replace(backup, runtime_app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
