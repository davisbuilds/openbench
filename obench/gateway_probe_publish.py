"""Fail-closed public bundles for completed Gateway Probe runs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import (
    gateway_probe_report,
    gateway_probe_results,
    gateway_probe_run,
    gateway_probe_spec,
    gateway_spec,
)
from .gateway_probe_models import GatewayProbeRunError, ProbeBlock


PUBLIC_SCHEMA_VERSION = 2
PUBLIC_BUNDLE_KIND = "gateway_probe_public"
PUBLIC_FILES = (
    "prices.json",
    "schedule.json",
    "results.jsonl",
    "report.json",
    "report.md",
)
SOURCE_FILES = (
    "experiment.toml",
    "prices.json",
    "results.jsonl",
    "report.json",
    "report.md",
)
_PUBLIC_DIRECTORY_FILES = frozenset((*PUBLIC_FILES, "manifest.json"))
_SOURCE_DIRECTORY_FILES = frozenset((*SOURCE_FILES, "manifest.json"))
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ACCOUNT_ID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", re.I)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:ghp|github_pat|xox[baprs])_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)
_FORBIDDEN_KEYS = {
    "accountid",
    "apikey",
    "authenv",
    "authorization",
    "credential",
    "endpoint",
    "gatewayid",
    "generationid",
    "privatecidrallowlist",
    "privatehostallowlist",
    "prompt",
    "requestbody",
    "responsebody",
    "secret",
    "transcript",
}
_PUBLIC_RESULT_FIELDS = (
    "schema_version",
    "benchmark",
    "cell_id",
    "identity",
    "expected_arm_ids",
    "scheduled_blocks_per_condition",
    "arm_role",
    "baseline",
    "model_match",
    "outcome",
    "route_integrity",
    "request_metrics",
    "reuse_evidence",
    "billing",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GatewayProbeRunError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise GatewayProbeRunError(f"JSON contains non-finite value {value}")


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GatewayProbeRunError(f"{label} is invalid JSON: {path}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_directory(path: Path, names: frozenset[str], label: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise GatewayProbeRunError(f"{label} must be a non-symlink directory")
    actual = {item.name for item in path.iterdir()}
    if actual != names:
        missing = sorted(names - actual)
        extra = sorted(actual - names)
        raise GatewayProbeRunError(
            f"{label} file set is not exact: missing={missing} extra={extra}"
        )
    for name in names:
        item = path / name
        if item.is_symlink() or not item.is_file():
            raise GatewayProbeRunError(
                f"{label} artifact must be a regular non-symlink file: {name}"
            )


def _sha256_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise GatewayProbeRunError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _commit_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise GatewayProbeRunError(f"{label} must be a full lowercase git commit")
    return value


def _assert_public_safe(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise GatewayProbeRunError(f"public data has a non-string key at {path}")
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized_key in _FORBIDDEN_KEYS:
                raise GatewayProbeRunError(
                    f"public data contains forbidden field {path}.{key}"
                )
            if key == "receipt_headers":
                if item != {}:
                    raise GatewayProbeRunError(
                        f"public data contains receipt values at {path}.{key}"
                    )
                continue
            _assert_public_safe(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_public_safe(item, f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    lowered = value.lower()
    if (
        lowered.startswith(("http://", "https://", "file://"))
        or value.startswith(("/", "../", "./"))
        or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        raise GatewayProbeRunError(f"public data contains a URL or path at {path}")
    if _ACCOUNT_ID_RE.search(value):
        raise GatewayProbeRunError(f"public data contains an account identifier at {path}")
    if _EMAIL_RE.search(value):
        raise GatewayProbeRunError(f"public data contains an account address at {path}")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise GatewayProbeRunError(f"public data contains a possible secret at {path}")


def _validate_source_manifest(run_dir: Path) -> dict[str, Any]:
    _exact_directory(run_dir, _SOURCE_DIRECTORY_FILES, "Gateway Probe run")
    manifest = _load_json(run_dir / "manifest.json", "source manifest")
    expected = {
        "schema_version",
        "benchmark",
        "result_schema_version",
        "experiment_id",
        "experiment_digest",
        "files",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected
        or manifest.get("schema_version") != 1
        or manifest.get("benchmark") != gateway_probe_results.BENCHMARK
        or manifest.get("result_schema_version")
        != gateway_probe_results.RESULT_SCHEMA_VERSION
        or not isinstance(manifest.get("experiment_id"), str)
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != set(SOURCE_FILES)
    ):
        raise GatewayProbeRunError("source manifest does not match Gateway Probe schema")
    _sha256_value(manifest.get("experiment_digest"), "source experiment_digest")
    for name in SOURCE_FILES:
        entry = manifest["files"].get(name)
        if not isinstance(entry, dict) or set(entry) != {"sha256"}:
            raise GatewayProbeRunError(f"source manifest has malformed entry for {name}")
        expected_digest = _sha256_value(entry.get("sha256"), f"source {name} sha256")
        if _sha256(run_dir / name) != expected_digest:
            raise GatewayProbeRunError(f"source artifact digest mismatch: {name}")
    return manifest


def _project_prices(value: Any) -> dict[str, Any]:
    expected_root = {"schema_version", "price_id", "currency", "prices"}
    if (
        not isinstance(value, dict)
        or set(value) != expected_root
        or value.get("schema_version") != 1
        or not isinstance(value.get("price_id"), str)
        or not value.get("price_id")
        or value.get("currency") != "USD"
        or not isinstance(value.get("prices"), list)
        or not value["prices"]
    ):
        raise GatewayProbeRunError("price snapshot does not match schema")
    items = []
    seen = set()
    fields = {
        "model",
        "input_per_million",
        "output_per_million",
        "effective_at",
        "currency",
    }
    for raw in value["prices"]:
        if (
            not isinstance(raw, dict)
            or set(raw) != fields
            or not isinstance(raw.get("model"), str)
            or not raw.get("model")
            or raw["model"] in seen
            or raw.get("currency") != "USD"
            or not isinstance(raw.get("effective_at"), str)
            or not raw.get("effective_at")
            or any(
                not isinstance(raw.get(name), str)
                for name in ("input_per_million", "output_per_million")
            )
        ):
            raise GatewayProbeRunError("price snapshot does not match schema")
        try:
            rates = (
                Decimal(raw["input_per_million"]),
                Decimal(raw["output_per_million"]),
            )
        except InvalidOperation as exc:
            raise GatewayProbeRunError(
                "price snapshot does not match schema"
            ) from exc
        if any(not rate.is_finite() or rate < 0 for rate in rates):
            raise GatewayProbeRunError("price snapshot does not match schema")
        seen.add(raw["model"])
        items.append({name: copy.deepcopy(raw[name]) for name in sorted(fields)})
    projected = {
        "schema_version": 1,
        "price_id": value["price_id"],
        "currency": "USD",
        "prices": items,
    }
    _assert_public_safe(projected)
    return projected


def _project_schedule(
    schedule: tuple[ProbeBlock, ...],
) -> dict[str, Any]:
    projected = {
        "schema_version": 1,
        "benchmark": gateway_probe_results.BENCHMARK,
        "blocks": [
            {
                "case_id": block.case_id,
                "prompt_digest": block.prompt_digest,
                "condition": block.condition,
                "repetition": block.repetition,
                "arm_ids": list(block.arm_ids),
            }
            for block in schedule
        ],
    }
    _validate_public_schedule(projected)
    return projected


def _validate_public_schedule(value: Any) -> tuple[ProbeBlock, ...]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "benchmark", "blocks"}
        or value.get("schema_version") != 1
        or value.get("benchmark") != gateway_probe_results.BENCHMARK
        or not isinstance(value.get("blocks"), list)
        or not value["blocks"]
    ):
        raise GatewayProbeRunError("public schedule does not match schema")
    blocks = []
    coordinates = set()
    for raw in value["blocks"]:
        if (
            not isinstance(raw, dict)
            or set(raw) != {
                "case_id",
                "prompt_digest",
                "condition",
                "repetition",
                "arm_ids",
            }
            or not isinstance(raw.get("case_id"), str)
            or not raw["case_id"]
            or raw.get("condition") not in gateway_probe_spec.CONDITIONS
            or not isinstance(raw.get("repetition"), int)
            or isinstance(raw.get("repetition"), bool)
            or raw["repetition"] < 1
            or not isinstance(raw.get("arm_ids"), list)
            or len(raw["arm_ids"]) < 2
            or any(
                not isinstance(arm_id, str) or not arm_id
                for arm_id in raw["arm_ids"]
            )
            or len(set(raw["arm_ids"])) != len(raw["arm_ids"])
        ):
            raise GatewayProbeRunError("public schedule block does not match schema")
        _sha256_value(raw.get("prompt_digest"), "public schedule prompt_digest")
        block = ProbeBlock(
            case_id=raw["case_id"],
            prompt_digest=raw["prompt_digest"],
            condition=raw["condition"],
            repetition=raw["repetition"],
            arm_ids=tuple(raw["arm_ids"]),
        )
        if block.coordinate in coordinates:
            raise GatewayProbeRunError(
                "public schedule contains duplicate coordinates"
            )
        coordinates.add(block.coordinate)
        blocks.append(block)
    arm_sets = {frozenset(block.arm_ids) for block in blocks}
    counts = {
        condition: sum(block.condition == condition for block in blocks)
        for condition in gateway_probe_spec.CONDITIONS
    }
    if len(arm_sets) != 1 or counts["cold"] != counts["warm"]:
        raise GatewayProbeRunError("public schedule is not matched across conditions")
    _assert_public_safe(value)
    return tuple(blocks)


def _validate_rows_against_public_schedule(
    rows: list[dict[str, Any]],
    schedule: tuple[ProbeBlock, ...],
    experiment_digest: str,
) -> None:
    blocks = {block.coordinate: block for block in schedule}
    for row in rows:
        identity = row["identity"]
        schedule_identity = identity["schedule"]
        coordinate = (
            identity["case"]["id"],
            schedule_identity["condition"],
            schedule_identity["repetition"],
        )
        block = blocks.get(coordinate)
        arm_id = identity["arm"]["id"]
        if (
            block is None
            or identity["case"]["prompt_digest"] != block.prompt_digest
            or arm_id not in block.arm_ids
            or row["expected_arm_ids"] != sorted(block.arm_ids)
            or schedule_identity["block_id"]
            != gateway_probe_results.block_id(
                experiment_digest,
                block,
                schedule_identity["block_attempt"],
            )
        ):
            raise GatewayProbeRunError(
                "public results do not match the authenticated schedule"
            )


def _project_result_row(row: Mapping[str, Any]) -> dict[str, Any]:
    gateway_probe_results.validate_row_shape(row)
    projected = {
        name: copy.deepcopy(row[name])
        for name in _PUBLIC_RESULT_FIELDS
    }
    projected["request_metrics"]["receipt_headers"] = {}
    projected["reuse_evidence"]["receipt_headers"] = {}
    route = projected["request_metrics"].get("route")
    if isinstance(route, dict) and isinstance(route.get("gateway_metadata"), dict):
        route["gateway_metadata"].pop("generationId", None)
    gateway_probe_results.validate_row_shape(projected)
    _assert_public_safe(projected)
    return projected


def _canonical_jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(
            row,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n"
        for row in rows
    )


def _detect_verifier_commit() -> str:
    source_root = Path(__file__).resolve().parents[1]
    try:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "status",
                "--porcelain",
                "--",
                "obench",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.stdout.strip():
            raise GatewayProbeRunError(
                "verifier source is dirty; pass --verified-with-commit explicitly"
            )
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except GatewayProbeRunError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise GatewayProbeRunError(
            "cannot detect verifier commit; pass --verified-with-commit"
        ) from exc
    return _commit_value(completed.stdout.strip(), "detected verifier commit")


def _verified_with_commit(explicit: str | None) -> str:
    if explicit is None:
        return _detect_verifier_commit()
    commit = _commit_value(explicit, "verified_with_commit")
    source_root = Path(__file__).resolve().parents[1]
    try:
        subprocess.run(
            ["git", "-C", str(source_root), "cat-file", "-e", f"{commit}^{{commit}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "cat-file",
                "-e",
                f"{commit}:obench/gateway_probe_publish.py",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GatewayProbeRunError(
            "verified_with_commit must resolve to a git commit containing "
            "obench/gateway_probe_publish.py"
        ) from exc
    return commit


def _validate_public_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise GatewayProbeRunError("public results must contain at least one row")
    for row in rows:
        gateway_probe_results.validate_row_shape(row)
        _assert_public_safe(row)
        if (
            row["request_metrics"]["receipt_headers"]
            or row["reuse_evidence"]["receipt_headers"]
        ):
            raise GatewayProbeRunError("public results contain receipt values")
        metadata = (row["request_metrics"].get("route") or {}).get(
            "gateway_metadata"
        )
        if isinstance(metadata, Mapping) and "generationId" in metadata:
            raise GatewayProbeRunError(
                "public results contain an operational generation identifier"
            )


def _validate_manifest(value: Any) -> dict[str, Any]:
    expected = {
        "schema_version",
        "bundle_kind",
        "benchmark",
        "result_schema_version",
        "report_schema_version",
        "experiment_id",
        "experiment_digest",
        "schedule_digest",
        "price_digest",
        "result_count",
        "complete_blocks",
        "scheduled_blocks_per_condition",
        "run_provenance",
        "verification",
        "sanitization",
        "files",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != PUBLIC_SCHEMA_VERSION
        or value.get("bundle_kind") != PUBLIC_BUNDLE_KIND
        or value.get("benchmark") != gateway_probe_results.BENCHMARK
        or value.get("result_schema_version")
        != gateway_probe_results.RESULT_SCHEMA_VERSION
        or value.get("report_schema_version")
        != gateway_probe_report.REPORT_SCHEMA_VERSION
        or not isinstance(value.get("experiment_id"), str)
        or not isinstance(value.get("result_count"), int)
        or isinstance(value.get("result_count"), bool)
        or value["result_count"] < 1
        or not isinstance(value.get("scheduled_blocks_per_condition"), int)
        or isinstance(value.get("scheduled_blocks_per_condition"), bool)
        or value["scheduled_blocks_per_condition"] < 1
        or not isinstance(value.get("complete_blocks"), dict)
        or set(value["complete_blocks"]) != {"cold", "warm"}
        or any(
            not isinstance(count, int)
            or isinstance(count, bool)
            or count != value["scheduled_blocks_per_condition"]
            for count in value["complete_blocks"].values()
        )
    ):
        raise GatewayProbeRunError("public manifest does not match schema")
    for name in ("experiment_digest", "schedule_digest", "price_digest"):
        _sha256_value(value.get(name), f"public manifest {name}")
    if value.get("run_provenance") != {
        "source_commit": "unknown",
        "started_at": "unknown",
        "completed_at": "unknown",
    }:
        raise GatewayProbeRunError("public run provenance must be explicitly unknown")
    verification = value.get("verification")
    if not isinstance(verification, dict) or set(verification) != {
        "verified_with_commit"
    }:
        raise GatewayProbeRunError("public verification provenance is malformed")
    _commit_value(verification.get("verified_with_commit"), "verified_with_commit")
    if value.get("sanitization") != {
        "case_prompts": "sha256_only",
        "receipt_values": "removed",
        "operational_identifiers": "removed",
        "paths_accounts_secrets": "rejected",
    }:
        raise GatewayProbeRunError("public sanitization declaration is malformed")
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != set(PUBLIC_FILES):
        raise GatewayProbeRunError("public manifest artifact set is malformed")
    for name, entry in files.items():
        if not isinstance(entry, dict) or set(entry) != {"sha256"}:
            raise GatewayProbeRunError(f"public manifest has malformed entry for {name}")
        _sha256_value(entry.get("sha256"), f"public {name} sha256")
    _assert_public_safe(value)
    return value


def _report_matches(
    report: Any,
    recomputed: dict[str, Any],
    markdown: str,
) -> None:
    if report != recomputed:
        raise GatewayProbeRunError("stored report.json does not match recomputed report")
    expected_markdown = gateway_probe_report.render_text(recomputed) + "\n"
    if markdown != expected_markdown:
        raise GatewayProbeRunError("stored report.md does not match recomputed report")


def _source_report_matches(
    report: Any,
    recomputed: dict[str, Any],
    markdown: str,
) -> None:
    if report == recomputed:
        _report_matches(report, recomputed, markdown)
        return
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 3
        or set(report) != (set(recomputed) | {"label"})
        or not isinstance(report.get("label"), str)
    ):
        raise GatewayProbeRunError(
            "stored source report.json does not match recomputed report"
        )
    migrated = copy.deepcopy(report)
    migrated.pop("label")
    migrated["schema_version"] = recomputed["schema_version"]
    if migrated != recomputed:
        raise GatewayProbeRunError(
            "stored source report.json does not match recomputed report"
        )
    lines = markdown.splitlines()
    if (
        not markdown.endswith("\n")
        or not lines
        or lines[0] != f"Gateway Probe ({report['label']})"
    ):
        raise GatewayProbeRunError(
            "stored source report.md does not match recomputed report"
        )
    lines[0] = "Gateway Probe"
    if "\n".join(lines) + "\n" != gateway_probe_report.render_text(recomputed) + "\n":
        raise GatewayProbeRunError(
            "stored source report.md does not match recomputed report"
        )


def _require_complete_report(report: Mapping[str, Any]) -> None:
    scheduled = report.get("scheduled_blocks_per_condition")
    complete = report.get("complete_blocks")
    if (
        not isinstance(scheduled, int)
        or isinstance(scheduled, bool)
        or scheduled < 1
        or not isinstance(complete, Mapping)
        or set(complete) != {"cold", "warm"}
        or any(complete[condition] != scheduled for condition in ("cold", "warm"))
    ):
        raise GatewayProbeRunError(
            "public Gateway Probe requires every scheduled cold and warm block"
        )


def _manifest_for(
    directory: Path,
    report: dict[str, Any],
    row_count: int,
    verified_with_commit: str,
) -> dict[str, Any]:
    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "bundle_kind": PUBLIC_BUNDLE_KIND,
        "benchmark": gateway_probe_results.BENCHMARK,
        "result_schema_version": gateway_probe_results.RESULT_SCHEMA_VERSION,
        "report_schema_version": report["schema_version"],
        "experiment_id": report["experiment_id"],
        "experiment_digest": report["experiment_digest"],
        "schedule_digest": report["schedule_digest"],
        "price_digest": report["price_digest"],
        "result_count": row_count,
        "complete_blocks": report["complete_blocks"],
        "scheduled_blocks_per_condition": report[
            "scheduled_blocks_per_condition"
        ],
        "run_provenance": {
            "source_commit": "unknown",
            "started_at": "unknown",
            "completed_at": "unknown",
        },
        "verification": {
            "verified_with_commit": verified_with_commit,
        },
        "sanitization": {
            "case_prompts": "sha256_only",
            "receipt_values": "removed",
            "operational_identifiers": "removed",
            "paths_accounts_secrets": "rejected",
        },
        "files": {
            name: {"sha256": _sha256(directory / name)}
            for name in PUBLIC_FILES
        },
    }


def publish_bundle(
    run_dir: str | os.PathLike[str],
    bundle_dir: str | os.PathLike[str],
    *,
    verified_with_commit: str | None = None,
) -> dict[str, Any]:
    source_input = Path(run_dir)
    destination_input = Path(bundle_dir)
    if source_input.is_symlink():
        raise GatewayProbeRunError("Gateway Probe run must not be a symlink")
    if destination_input.is_symlink():
        raise GatewayProbeRunError("public bundle destination must not be a symlink")
    source = source_input.resolve()
    destination = destination_input.resolve()
    if destination.exists():
        raise GatewayProbeRunError(f"public bundle already exists: {destination}")
    source_manifest = _validate_source_manifest(source)
    experiment = gateway_probe_spec.load_experiment(source / "experiment.toml")
    if (
        experiment.experiment_id != source_manifest["experiment_id"]
        or experiment.digest != source_manifest["experiment_digest"]
    ):
        raise GatewayProbeRunError("source experiment does not match source manifest")
    prices = _project_prices(_load_json(source / "prices.json", "source prices"))
    schedule = gateway_probe_run.build_schedule(experiment)
    projected_schedule = _project_schedule(schedule)
    schedule_digest = gateway_spec.canonical_digest(
        projected_schedule["blocks"]
    )
    price_digest = gateway_spec.canonical_digest(prices)
    rows = gateway_probe_results.load_results(source / "results.jsonl")
    gateway_probe_results.validate_resume_rows(
        rows,
        experiment=experiment,
        schedule=schedule,
        schedule_digest=schedule_digest,
        price_digest=price_digest,
    )
    recomputed = gateway_probe_report.aggregate(rows, experiment=experiment)
    _require_complete_report(recomputed)
    _source_report_matches(
        _load_json(source / "report.json", "source report"),
        recomputed,
        (source / "report.md").read_text(encoding="utf-8"),
    )
    if price_digest != recomputed["price_digest"]:
        raise GatewayProbeRunError("source prices do not match results price_digest")
    if schedule_digest != recomputed["schedule_digest"]:
        raise GatewayProbeRunError(
            "source schedule does not match results schedule_digest"
        )
    projected_rows = [_project_result_row(row) for row in rows]
    projected_report = gateway_probe_report.aggregate(projected_rows)
    if projected_report != recomputed:
        raise GatewayProbeRunError("public projection changes report aggregation")
    commit = _verified_with_commit(verified_with_commit)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    ))
    try:
        (temporary / "prices.json").write_text(
            _canonical_json(prices),
            encoding="ascii",
        )
        (temporary / "schedule.json").write_text(
            _canonical_json(projected_schedule),
            encoding="ascii",
        )
        (temporary / "results.jsonl").write_text(
            _canonical_jsonl(projected_rows),
            encoding="ascii",
        )
        (temporary / "report.json").write_text(
            _canonical_json(projected_report),
            encoding="ascii",
        )
        (temporary / "report.md").write_text(
            gateway_probe_report.render_text(projected_report) + "\n",
            encoding="utf-8",
        )
        manifest = _manifest_for(
            temporary,
            projected_report,
            len(projected_rows),
            commit,
        )
        (temporary / "manifest.json").write_text(
            _canonical_json(manifest),
            encoding="ascii",
        )
        verify_bundle(temporary)
        os.replace(temporary, destination)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_bundle(bundle_dir: str | os.PathLike[str]) -> dict[str, Any]:
    bundle_input = Path(bundle_dir)
    if bundle_input.is_symlink():
        raise GatewayProbeRunError("public Gateway Probe bundle must not be a symlink")
    directory = bundle_input.resolve()
    _exact_directory(directory, _PUBLIC_DIRECTORY_FILES, "public Gateway Probe bundle")
    manifest = _validate_manifest(_load_json(directory / "manifest.json", "manifest"))
    for name in PUBLIC_FILES:
        if _sha256(directory / name) != manifest["files"][name]["sha256"]:
            raise GatewayProbeRunError(f"public artifact digest mismatch: {name}")

    prices = _project_prices(_load_json(directory / "prices.json", "public prices"))
    schedule_value = _load_json(directory / "schedule.json", "public schedule")
    schedule = _validate_public_schedule(schedule_value)
    rows = gateway_probe_results.load_results(directory / "results.jsonl")
    _validate_public_rows(rows)
    recomputed = gateway_probe_report.aggregate(rows)
    _require_complete_report(recomputed)
    if (
        gateway_spec.canonical_digest(schedule_value["blocks"])
        != recomputed["schedule_digest"]
    ):
        raise GatewayProbeRunError("public schedule does not match results")
    if any(
        sum(block.condition == condition for block in schedule)
        != recomputed["scheduled_blocks_per_condition"]
        for condition in gateway_probe_spec.CONDITIONS
    ):
        raise GatewayProbeRunError(
            "public schedule count does not match results"
        )
    _validate_rows_against_public_schedule(
        rows,
        schedule,
        recomputed["experiment_digest"],
    )
    _report_matches(
        _load_json(directory / "report.json", "public report"),
        recomputed,
        (directory / "report.md").read_text(encoding="utf-8"),
    )

    bindings = {
        "experiment_id": recomputed["experiment_id"],
        "experiment_digest": recomputed["experiment_digest"],
        "schedule_digest": recomputed["schedule_digest"],
        "price_digest": recomputed["price_digest"],
        "result_count": len(rows),
        "complete_blocks": recomputed["complete_blocks"],
        "scheduled_blocks_per_condition": recomputed[
            "scheduled_blocks_per_condition"
        ],
        "report_schema_version": recomputed["schema_version"],
    }
    for name, expected in bindings.items():
        if manifest[name] != expected:
            raise GatewayProbeRunError(
                f"public manifest {name} does not match recomputed report"
            )
    if gateway_spec.canonical_digest(prices) != recomputed["price_digest"]:
        raise GatewayProbeRunError("public prices do not match results")
    return manifest
