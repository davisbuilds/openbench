#!/usr/bin/env python3
"""Publish and verify shareable OpenBench comparison bundles.

``obench publish`` turns gate-passing candidate runs into a self-contained
artifact (HTML card + filtered results + provenance + README) that a third
party can post. Transcripts are never bundled. ``obench verify`` recomputes
digests recorded in provenance.json so shared claims are tamper-evident
without a server.

What verify proves: the bundled results.jsonl still matches its recorded
SHA-256, and per-task content digests still match when the referenced task
trees are available. What it does NOT prove: runs were not cherry-picked or
rerun until green — publish the full matrix.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone

from . import __version__
from . import compare
from . import report_page
from . import scrub
from . import stats
from .harbor_results import (
    expected_harbor_agent_semantic_name,
    harbor_exception_semantics,
)
from .harbor_profiles import (
    expected_harbor_model_name,
)
from .harbor_metering import publication_decision
from .harbor_job import (
    COMPARISON_PLAN_SCHEMA_VERSION,
    canonical_comparison_plan_bytes,
)
from .paths import default_results_path, default_tasks_dir, resolve_tasks_dir
from .run import make_run_id
from .usage_evidence import harbor_usage_policy

TRANSCRIPT_FIELD_KEYS = (
    "transcript_path",
    "transcript",
    "transcripts_dir",
    "full_output",
    "output_tail",
    "checker_stdout",
    "checker_stderr",
)
# Injected by stats.load_rows; absolute paths here trip the publish PII check.
LOAD_META_FIELD_KEYS = ("_source", "_lineno")
# High-signal PII classes for publish bundles. Hex/base64 catch-alls are omitted
# because provenance digests and image digests are intentional 32+ hex strings.
PUBLISH_PII_CATEGORIES = frozenset({
    "email", "api-key", "github-token", "slack-token", "aws-key",
    "home-path", "username", "hostname",
})
HARBOR_PROVENANCE_KEYS = frozenset({
    "kind",
    "harbor_version",
    "harbor_git_commit_hash",
    "harbor_job_id",
    "harbor_trial_id",
    "harbor_trial_name",
    "job_lock_sha256",
    "job_result_sha256",
    "trial_lock_sha256",
    "trial_result_sha256",
    "reward_sha256",
    "openbench_verifier_evidence_sha256",
    "atif_sha256",
    "artifact_manifest_sha256",
    "final_workspace_sha256",
    "task_digest",
    "openbench_task_content_digest",
    "openbench_harbor_export",
    "harbor_task_checksum",
    "harbor_agent_config_name",
    "harbor_model_name",
    "agent_config_sha256",
    "harbor_verifier_time_s",
    "harbor_job_retries",
    "harbor_job_max_retries",
    "harbor_exception_type",
    "comparison_plan_schema_version",
    "comparison_plan_sha256",
    "comparison_plan",
    "comparison_arm_id",
    "comparison_resolved_tasks",
    "comparison_block",
    "usage_source",
    "proxy_measured",
    "harbor_metering",
    "trial_mapping",
    "temporal_matched_block_claim",
})
HARBOR_SUITE_PROVENANCE_KEYS = frozenset({
    "suite_manifest_schema_version",
    "suite_manifest_sha256",
    "suite_manifest",
    "suite_task_set_id",
    "suite_publication_scope",
    "suite_completeness",
})
HARBOR_CORE_DIGEST_KEYS = frozenset({
    "job_lock_sha256",
    "job_result_sha256",
    "trial_lock_sha256",
    "trial_result_sha256",
    "harbor_task_checksum",
})
HARBOR_OPTIONAL_DIGEST_KEYS = frozenset({
    "reward_sha256",
    "openbench_verifier_evidence_sha256",
    "atif_sha256",
    "artifact_manifest_sha256",
    "final_workspace_sha256",
})
HARBOR_DIGEST_KEYS = HARBOR_CORE_DIGEST_KEYS | HARBOR_OPTIONAL_DIGEST_KEYS
HARBOR_MARKER_KEYS = HARBOR_DIGEST_KEYS | frozenset({
    "harbor_version",
    "harbor_git_commit_hash",
    "harbor_job_id",
    "harbor_trial_id",
    "harbor_trial_name",
    "openbench_task_content_digest",
    "openbench_harbor_export",
    "harbor_agent_config_name",
    "harbor_model_name",
    "harbor_verifier_time_s",
    "harbor_job_retries",
    "harbor_job_max_retries",
})
HARBOR_PUBLISH_ROW_KEYS = (
    "run_id", "ts_iso", "harness", "model", "task", "trial",
    "success", "completed", "error", "wall_time_s", "t_env_setup_s",
    "t_agent_s", "t_checker_s", "tokens", "tokens_input_uncached",
    "tokens_cache_read", "tokens_cache_write", "tokens_output",
    "tokens_reasoning", "usage_raw", "token_basis", "tokens_fresh", "turns",
    "checker_exit", "exec_mode", "score", "harness_version",
    "harness_version_source", "failure_class", "candidate_provenance",
    "failure_reason", "version_drift", "workspace_source",
    "tokens_proxy_input_uncached", "tokens_proxy_cache_read",
    "tokens_proxy_cache_write", "tokens_proxy_output",
    "tokens_proxy_reasoning", "tokens_proxy_calls", "token_basis_proxy",
    "proxy_capture_truncated",
    "usage_evidence_grade", "usage_ranking_eligible",
    "usage_ranking_exclusion_reason",
)
HARBOR_PROXY_ROW_KEYS = (
    "tokens_proxy_input_uncached", "tokens_proxy_cache_read",
    "tokens_proxy_cache_write", "tokens_proxy_output",
    "tokens_proxy_reasoning", "tokens_proxy_calls", "token_basis_proxy",
    "proxy_capture_truncated",
)
PUBLISH_METHODOLOGY = """## What this card is

A self-contained OpenBench comparison bundle: candidate harness arm(s)
highlighted against stock arms on the same result rows.

## What `obench verify` proves

- The bundled `results.jsonl` still matches the SHA-256 recorded in
  `provenance.json` (tamper-evident).
- When local task trees are available, per-task content digests still
  match under the bundle's digest scheme (scheme 2: instruction.md +
  checker.sh + workspace|workspace.toml + checker_data/; scheme 1 /
  legacy: same without checker_data/). Missing digests FAIL verification.

## What verify does NOT prove

- Runs were not cherry-picked or rerun until green.
- Auth material, secrets, or LOCAL-ONLY transcripts were reviewed
  (transcripts are never included in the bundle).
- Candidate admission-gate live checks were executed on this machine.

Publish the full matrix whenever possible.
"""


class PublishError(ValueError):
    """User-facing publish failure."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# Digest schemes for task_content_digest / provenance.json:
#   1 (legacy) — instruction.md + checker.sh + workspace.toml + workspace/
#                (no checker_data/). Bundles without digest_scheme use this.
#   2 (current) — scheme 1 plus checker_data/ (oracle inputs).
DIGEST_SCHEME_LEGACY = 1
DIGEST_SCHEME_CURRENT = 2


def _feed_tree_into_digest(hasher, task_dir: str, tree_name: str, feed) -> None:
    """Hash every regular file under ``task_dir/tree_name`` in stable order."""
    root_dir = os.path.join(task_dir, tree_name)
    if not os.path.isdir(root_dir):
        return
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            rel = os.path.relpath(full, task_dir).replace(os.sep, "/")
            with open(full, "rb") as fh:
                feed(rel, fh.read())


def resolve_digest_scheme(provenance) -> int:
    """Return the digest scheme for a provenance dict (missing → legacy 1)."""
    raw = provenance.get("digest_scheme") if isinstance(provenance, dict) else None
    if raw is None:
        return DIGEST_SCHEME_LEGACY
    try:
        scheme = int(raw)
    except (TypeError, ValueError) as exc:
        raise PublishError(f"invalid digest_scheme in provenance: {raw!r}") from exc
    if scheme not in (DIGEST_SCHEME_LEGACY, DIGEST_SCHEME_CURRENT):
        raise PublishError(f"unsupported digest_scheme: {scheme}")
    return scheme


def task_content_digest(task_dir: str, scheme: int = DIGEST_SCHEME_CURRENT) -> str:
    """SHA-256 over task oracle inputs under the given digest scheme.

    Scheme 1 (legacy): instruction.md + checker.sh + workspace.toml + workspace/.
    Scheme 2 (current): scheme 1 plus ``checker_data/`` so post-publish changes
    to checker-owned fixtures fail verify.

    Files are hashed in a stable path-sorted order. Missing optional pieces are
    skipped; at least instruction.md or checker.sh must exist.
    """
    if scheme not in (DIGEST_SCHEME_LEGACY, DIGEST_SCHEME_CURRENT):
        raise PublishError(f"unsupported digest_scheme: {scheme}")
    task_dir = os.path.abspath(task_dir)
    hasher = hashlib.sha256()
    found = False

    def _feed(rel: str, data: bytes):
        nonlocal found
        found = True
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(data)
        hasher.update(b"\0")

    for name in ("instruction.md", "checker.sh", "workspace.toml"):
        path = os.path.join(task_dir, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                _feed(name, fh.read())

    _feed_tree_into_digest(hasher, task_dir, "workspace", _feed)
    if scheme >= DIGEST_SCHEME_CURRENT:
        _feed_tree_into_digest(hasher, task_dir, "checker_data", _feed)

    if not found:
        raise PublishError(f"task dir has no hashable content: {task_dir}")
    return hasher.hexdigest()


def resolve_task_dir(task: str, tasks_dirs):
    """Return the first existing task directory under ``tasks_dirs``."""
    roots = stats.parse_tasks_dirs(tasks_dirs)
    for root in roots:
        path = os.path.join(root, task)
        if os.path.isdir(path):
            return path
    return None


def _arm_name(row):
    return compare._row_arm(row)


def _is_candidate_row(row):
    return isinstance(row.get("candidate_provenance"), dict)


def _candidate_name(row):
    prov = row.get("candidate_provenance")
    if isinstance(prov, dict) and prov.get("name"):
        return str(prov["name"])
    return None


def resolve_highlight_names(candidate_specs):
    """Map ``--candidate`` path-or-name args to highlight arm names."""
    names = []
    for spec in candidate_specs or []:
        from .candidates import load_candidate, resolve_candidate_path
        from .paths import default_adapters_dir
        if os.path.isfile(spec):
            try:
                names.append(load_candidate(spec, default_adapters_dir()).name)
            except Exception as exc:  # noqa: BLE001 - surface as publish error
                raise PublishError(f"could not load candidate {spec!r}: {exc}") from exc
            continue
        try:
            path = resolve_candidate_path(spec)
            names.append(load_candidate(path, default_adapters_dir()).name)
        except (OSError, ValueError, KeyError):
            # Bare harness/candidate name for highlight matching.
            names.append(str(spec))
    return names


def strip_transcript_fields(row):
    """Return a shallow copy without LOCAL-ONLY transcript-bearing fields."""
    drop = set(TRANSCRIPT_FIELD_KEYS) | set(LOAD_META_FIELD_KEYS)
    cleaned = {key: value for key, value in row.items() if key not in drop}
    return cleaned


def _redact_local_path(value):
    if not isinstance(value, str) or not value:
        return value
    if value in (".",):
        return value
    if value.startswith(("http://", "https://", "git@", "ssh://")):
        return value
    if value.startswith("~/"):
        return value
    if os.path.isabs(value) or (os.sep in value) or ("/" in value):
        base = os.path.basename(value.rstrip("/\\"))
        return base or "<path>"
    return value


def _is_harbor_row(row):
    provenance = row.get("candidate_provenance")
    workspace = row.get("workspace_source")
    provenance_keys = set(provenance) if isinstance(provenance, dict) else set()
    return (
        row.get("exec_mode") == "harbor"
        or (isinstance(provenance, dict) and provenance.get("kind") == "harbor_job")
        or bool(provenance_keys & HARBOR_MARKER_KEYS)
        or (isinstance(workspace, dict) and workspace.get("kind") == "harbor_artifact")
    )


def _is_nonnegative_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _valid_comparison_dataset_descriptor(value):
    if not isinstance(value, dict):
        return False
    name = value.get("name")
    if not isinstance(name, str) or not name:
        return False
    identity_field = "ref" if "/" in name else "version"
    expected_fields = {"name", identity_field}
    if "task_names" in value:
        expected_fields.add("task_names")
    if set(value) != expected_fields:
        return False
    identity = value.get(identity_field)
    if identity_field == "ref":
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None
        ):
            return False
    elif not isinstance(identity, str) or not identity:
        return False
    task_names = value.get("task_names")
    if "task_names" not in value:
        return True
    return (
        isinstance(task_names, list)
        and bool(task_names)
        and all(isinstance(task, str) and bool(task) for task in task_names)
        and len(task_names) == len(set(task_names))
    )


def _validate_harbor_usage(row, provenance):
    basis = row.get("token_basis")
    if basis not in ("harbor_agent_reported", "unmetered"):
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: unsupported token_basis {basis!r}"
        )
    if provenance["usage_source"] != basis:
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: usage_source does not "
            "match token_basis"
        )
    nonempty_proxy = [key for key in HARBOR_PROXY_ROW_KEYS if row.get(key) is not None]
    metering = provenance["harbor_metering"]
    reconciliation_status = None
    proxy_required = False
    if provenance["proxy_measured"] is False:
        if metering is not None or nonempty_proxy:
            raise PublishError(
                f"Harbor row {row.get('run_id', '?')!r}: unmetered proxy "
                "evidence must be null"
            )
    elif provenance["proxy_measured"] is True:
        expected_metering_keys = {
            "schema_version",
            "reconciliation_status",
            "ledger_root_hash",
            "ledger_record_count",
            "model_call_count",
            "auxiliary_request_count",
            "publication",
            "proxy_required",
            "evidence_sha256",
            "ledger_sha256",
        }
        if not isinstance(metering, dict) or set(metering) != expected_metering_keys:
            raise PublishError(
                f"Harbor row {row.get('run_id', '?')!r}: malformed metering evidence"
            )
        reconciliation_status = metering.get("reconciliation_status")
        proxy_required = metering.get("proxy_required") is True
        expected_publication = publication_decision(
            {"reconciliation": {"status": reconciliation_status}},
            proxy_required=proxy_required,
        )
        if (
            metering["schema_version"] != "openbench.harbor-metering.v2"
            or reconciliation_status not in ("exact", "mismatch")
            or not proxy_required
            or metering["publication"] != expected_publication
        ):
            raise PublishError(
                f"Harbor row {row.get('run_id', '?')!r}: proxy metering is "
                "not publication-eligible"
            )
        for key in (
            "ledger_record_count",
            "model_call_count",
            "auxiliary_request_count",
        ):
            if (
                not isinstance(metering[key], int)
                or isinstance(metering[key], bool)
                or metering[key] < 0
            ):
                raise PublishError(
                    f"Harbor row {row.get('run_id', '?')!r}: invalid "
                    f"metering {key}"
                )
        if (
            metering["ledger_record_count"]
            != metering["model_call_count"]
            + metering["auxiliary_request_count"]
            or metering["model_call_count"] != row.get("tokens_proxy_calls")
        ):
            raise PublishError(
                f"Harbor row {row.get('run_id', '?')!r}: metering request "
                "counts disagree"
            )
        for key in ("ledger_root_hash", "evidence_sha256", "ledger_sha256"):
            if not isinstance(metering[key], str) or re.fullmatch(
                r"[0-9a-f]{64}", metering[key]
            ) is None:
                raise PublishError(
                    f"Harbor row {row.get('run_id', '?')!r}: invalid metering {key}"
                )
        proxy_int_fields = (
            "tokens_proxy_calls",
            "tokens_proxy_input_uncached",
            "tokens_proxy_cache_read",
            "tokens_proxy_output",
        )
        if any(
            not isinstance(row.get(key), int)
            or isinstance(row.get(key), bool)
            or row.get(key) < 0
            for key in proxy_int_fields
        ):
            raise PublishError(
                f"Harbor row {row.get('run_id', '?')!r}: incomplete proxy totals"
            )
        if (
            row.get("token_basis_proxy") != "proxy_measured"
            or row.get("tokens_proxy_cache_write") is not None
            or row.get("tokens_proxy_reasoning") is not None
            or row.get("proxy_capture_truncated") not in (None, False)
        ):
            raise PublishError(
                f"Harbor row {row.get('run_id', '?')!r}: unsupported proxy fields"
            )
    else:
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: proxy_measured must be boolean"
        )

    expected_policy = harbor_usage_policy(
        basis,
        proxy_required=proxy_required,
        reconciliation_status=reconciliation_status,
    )
    actual_policy = (
        row.get("usage_evidence_grade"),
        row.get("usage_ranking_eligible"),
        row.get("usage_ranking_exclusion_reason"),
    )
    if actual_policy != expected_policy:
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: usage evidence policy "
            "fields disagree"
        )

    usage = row.get("usage_raw")
    usage_fields = (
        "tokens", "tokens_input_uncached", "tokens_cache_read",
        "tokens_output", "tokens_fresh",
    )
    if basis == "unmetered":
        nonempty = [key for key in usage_fields if row.get(key) is not None]
        if usage is not None or nonempty:
            raise PublishError(
                f"Harbor row {row.get('run_id', '?')!r}: unmetered usage "
                "must not contain token counts"
            )
        return

    expected_usage_keys = {
        "source", "n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd"
    }
    if not isinstance(usage, dict) or set(usage) != expected_usage_keys:
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: malformed usage_raw"
        )
    if usage["source"] != "harbor_agent_result":
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: invalid usage_raw source"
        )
    for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
        value = usage[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PublishError(
                f"Harbor row {row.get('run_id', '?')!r}: invalid usage_raw {key}"
            )
    cost = usage["cost_usd"]
    if cost is not None and not _is_nonnegative_number(cost):
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: invalid usage_raw cost_usd"
        )
    input_tokens = usage["n_input_tokens"]
    cache_tokens = usage["n_cache_tokens"]
    output_tokens = usage["n_output_tokens"]
    if cache_tokens > input_tokens:
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: cache tokens exceed input"
        )
    uncached = input_tokens - cache_tokens
    expected = {
        "tokens": uncached + output_tokens,
        "tokens_input_uncached": uncached,
        "tokens_cache_read": cache_tokens,
        "tokens_output": output_tokens,
        "tokens_fresh": uncached + output_tokens,
    }
    mismatched = [key for key, value in expected.items() if row.get(key) != value]
    if mismatched:
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: usage totals disagree: "
            + ", ".join(sorted(mismatched))
        )
    if row.get("tokens_cache_write") is not None or row.get("tokens_reasoning") is not None:
        raise PublishError(
            f"Harbor row {row.get('run_id', '?')!r}: unsupported usage fields "
            "must be null"
        )
    if provenance["proxy_measured"] is True and reconciliation_status == "exact":
        expected_proxy = {
            "tokens_proxy_input_uncached": row.get("tokens_input_uncached"),
            "tokens_proxy_cache_read": row.get("tokens_cache_read"),
            "tokens_proxy_output": row.get("tokens_output"),
        }
        if any(row.get(key) != value for key, value in expected_proxy.items()):
            raise PublishError(
                f"Harbor row {row.get('run_id', '?')!r}: proxy totals disagree "
                "with exact agent reconciliation"
            )


def _validate_harbor_row(row):
    run_id = row.get("run_id", "?")
    provenance = row.get("candidate_provenance")
    workspace = row.get("workspace_source")
    if row.get("exec_mode") != "harbor":
        raise PublishError(f"Harbor row {run_id!r}: exec_mode must be 'harbor'")
    provenance_keys = set(provenance) if isinstance(provenance, dict) else set()
    expected_provenance_keys = HARBOR_PROVENANCE_KEYS
    if provenance_keys & HARBOR_SUITE_PROVENANCE_KEYS:
        expected_provenance_keys |= HARBOR_SUITE_PROVENANCE_KEYS
    if provenance_keys != expected_provenance_keys:
        missing = sorted(expected_provenance_keys - provenance_keys)
        extra = sorted(provenance_keys - expected_provenance_keys)
        raise PublishError(
            f"Harbor row {run_id!r}: malformed candidate_provenance "
            f"(missing={missing}, extra={extra})"
        )
    if provenance["kind"] != "harbor_job":
        raise PublishError(
            f"Harbor row {run_id!r}: candidate_provenance.kind must be 'harbor_job'"
        )
    for key in ("harbor_job_retries", "harbor_job_max_retries"):
        value = provenance[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PublishError(f"Harbor row {run_id!r}: invalid {key}")
    exception_type = provenance["harbor_exception_type"]
    terminal_failure = exception_type is not None
    if terminal_failure and (
        not isinstance(exception_type, str)
        or not exception_type
        or "/" in exception_type
        or "\\" in exception_type
    ):
        raise PublishError(f"Harbor row {run_id!r}: invalid harbor_exception_type")
    for key in HARBOR_CORE_DIGEST_KEYS:
        if not isinstance(provenance[key], str) or re.fullmatch(
                r"[0-9a-f]{64}", provenance[key]) is None:
            raise PublishError(f"Harbor row {run_id!r}: invalid {key}")
    for key in HARBOR_OPTIONAL_DIGEST_KEYS:
        value = provenance[key]
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
        ):
            raise PublishError(f"Harbor row {run_id!r}: invalid {key}")
        if not terminal_failure and value is None:
            raise PublishError(f"Harbor row {run_id!r}: missing {key}")
    if not isinstance(provenance["task_digest"], str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", provenance["task_digest"]) is None:
        raise PublishError(f"Harbor row {run_id!r}: invalid task_digest")
    content_digest = provenance["openbench_task_content_digest"]
    if content_digest is not None and (
        not isinstance(content_digest, dict)
        or set(content_digest) != {"scheme", "sha256"}
        or content_digest.get("scheme") != DIGEST_SCHEME_CURRENT
        or isinstance(content_digest.get("scheme"), bool)
        or not isinstance(content_digest.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", content_digest["sha256"]) is None
    ):
        raise PublishError(
            f"Harbor row {run_id!r}: invalid openbench_task_content_digest"
        )
    export_config = provenance["openbench_harbor_export"]
    if export_config is not None and (
        not isinstance(export_config, dict)
        or set(export_config)
        != {"schema_version", "base_image", "network_mode"}
        or export_config.get("schema_version") != 1
        or isinstance(export_config.get("schema_version"), bool)
        or not isinstance(export_config.get("base_image"), str)
        or not export_config["base_image"]
        or not isinstance(export_config.get("network_mode"), str)
        or not export_config["network_mode"]
    ):
        raise PublishError(
            f"Harbor row {run_id!r}: invalid openbench_harbor_export"
        )
    if (content_digest is None) != (export_config is None):
        raise PublishError(
            f"Harbor row {run_id!r}: partial OpenBench verifier binding"
        )
    if not terminal_failure and content_digest is None:
        raise PublishError(
            f"Harbor row {run_id!r}: missing OpenBench verifier binding"
        )
    if not isinstance(provenance["harbor_git_commit_hash"], str) or re.fullmatch(
            r"[0-9a-f]{40}", provenance["harbor_git_commit_hash"]) is None:
        raise PublishError(f"Harbor row {run_id!r}: invalid harbor_git_commit_hash")
    for key in (
        "harbor_version", "harbor_job_id", "harbor_trial_id",
        "harbor_trial_name", "harbor_agent_config_name",
    ):
        value = provenance[key]
        if (
            not isinstance(value, str)
            or not value
            or "/" in value
            or "\\" in value
            or value.startswith("~")
        ):
            raise PublishError(f"Harbor row {run_id!r}: invalid {key}")
    harbor_model_name = provenance["harbor_model_name"]
    if (
        not isinstance(harbor_model_name, str)
        or not harbor_model_name
        or "\\" in harbor_model_name
        or harbor_model_name.startswith("~")
    ):
        raise PublishError(f"Harbor row {run_id!r}: invalid harbor_model_name")
    if (
        provenance["harbor_verifier_time_s"] is not None
        and not _is_nonnegative_number(provenance["harbor_verifier_time_s"])
    ):
        raise PublishError(f"Harbor row {run_id!r}: invalid harbor_verifier_time_s")
    if not terminal_failure and provenance["harbor_verifier_time_s"] is None:
        raise PublishError(f"Harbor row {run_id!r}: missing harbor_verifier_time_s")
    comparison_values = (
        provenance["comparison_plan_schema_version"],
        provenance["comparison_plan_sha256"],
        provenance["comparison_plan"],
        provenance["comparison_arm_id"],
        provenance["agent_config_sha256"],
        provenance["comparison_resolved_tasks"],
        provenance["comparison_block"],
    )
    has_comparison_plan = any(value is not None for value in comparison_values)
    if has_comparison_plan:
        if any(value is None for value in comparison_values):
            raise PublishError(
                f"Harbor row {run_id!r}: partial comparison identity"
            )
        if (
            provenance["comparison_plan_schema_version"]
            != COMPARISON_PLAN_SCHEMA_VERSION
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: invalid comparison plan schema"
            )
        if (
            not isinstance(provenance["comparison_plan_sha256"], str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                provenance["comparison_plan_sha256"],
            )
            is None
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: invalid comparison plan digest"
            )
        comparison_plan = provenance["comparison_plan"]
        expected_plan_fields = {
            "schema_version",
            "harbor_version",
            "harbor_git_commit_hash",
            "job_name",
            "submitted_job_config_sha256",
            "effective_job_config_sha256",
            "attempts",
            "dataset",
            "tasks",
            "arms",
        }
        if (
            not isinstance(comparison_plan, dict)
            or set(comparison_plan) != expected_plan_fields
            or comparison_plan.get("schema_version")
            != COMPARISON_PLAN_SCHEMA_VERSION
            or comparison_plan.get("harbor_version")
            != provenance["harbor_version"]
            or comparison_plan.get("harbor_git_commit_hash")
            != provenance["harbor_git_commit_hash"]
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: invalid comparison plan manifest"
            )
        try:
            canonical_plan = canonical_comparison_plan_bytes(comparison_plan)
        except (TypeError, ValueError) as exc:
            raise PublishError(
                f"Harbor row {run_id!r}: invalid comparison plan manifest"
            ) from exc
        if (
            hashlib.sha256(canonical_plan).hexdigest()
            != provenance["comparison_plan_sha256"]
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: comparison plan digest disagrees"
            )
        job_name = comparison_plan.get("job_name")
        submitted_config_sha256 = comparison_plan.get(
            "submitted_job_config_sha256"
        )
        effective_config_sha256 = comparison_plan.get(
            "effective_job_config_sha256"
        )
        attempts = comparison_plan.get("attempts")
        dataset = comparison_plan.get("dataset")
        tasks = comparison_plan.get("tasks")
        arms = comparison_plan.get("arms")
        if (
            not isinstance(job_name, str)
            or not job_name
            or "/" in job_name
            or "\\" in job_name
            or not isinstance(submitted_config_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", submitted_config_sha256) is None
            or not isinstance(effective_config_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", effective_config_sha256) is None
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < 1
            or (
                tasks is not None
                and (
                    not isinstance(tasks, list)
                    or not tasks
                    or tasks != sorted(set(tasks))
                    or any(
                        not isinstance(task, str) or not task
                        for task in tasks
                    )
                )
            )
            or (
                dataset is not None
                and not _valid_comparison_dataset_descriptor(dataset)
            )
            or (dataset is None) == (tasks is None)
            or not isinstance(arms, list)
            or not arms
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: invalid comparison plan manifest"
            )
        arm_id = provenance["comparison_arm_id"]
        agent_config_sha256 = provenance["agent_config_sha256"]
        resolved_tasks = provenance["comparison_resolved_tasks"]
        if (
            not isinstance(arm_id, str)
            or not arm_id
            or "\\" in arm_id
            or "\x00" in arm_id
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: invalid comparison arm identity"
            )
        if (
            not isinstance(agent_config_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", agent_config_sha256) is None
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: invalid rendered agent config digest"
            )
        if (
            not isinstance(resolved_tasks, list)
            or not resolved_tasks
            or resolved_tasks != sorted(set(resolved_tasks))
            or any(not isinstance(task, str) or not task for task in resolved_tasks)
            or (tasks is not None and resolved_tasks != tasks)
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: invalid resolved comparison task set"
            )
        matching_arms = [
            arm
            for arm in arms
            if (
                isinstance(arm, dict)
                and set(arm)
                == {
                    "arm_id",
                    "agent_config_name",
                    "harbor_model_name",
                    "agent_config_sha256",
                    "canonical_harness",
                    "canonical_model",
                }
                and arm.get("arm_id") == arm_id
                and arm.get("agent_config_name")
                == provenance["harbor_agent_config_name"]
                and arm.get("harbor_model_name")
                == provenance["harbor_model_name"]
                and arm.get("agent_config_sha256") == agent_config_sha256
                and arm.get("canonical_harness") == row.get("harness")
                and arm.get("canonical_model") == row.get("model")
            )
        ]
        if len(matching_arms) != 1:
            raise PublishError(
                f"Harbor row {run_id!r}: comparison arm does not match plan"
            )
        block = provenance["comparison_block"]
        if (
            not isinstance(block, dict)
            or set(block) != {"task", "index"}
            or block.get("task") != row.get("task")
            or block.get("index") != row.get("trial")
            or block.get("task") not in resolved_tasks
            or not isinstance(block.get("index"), int)
            or isinstance(block.get("index"), bool)
            or block["index"] < 1
            or block["index"] > attempts
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: invalid comparison block identity"
            )
        expected_run_id = make_run_id(
            row.get("harness"),
            row.get("task"),
            row.get("model"),
            row.get("trial"),
            candidate_digest=agent_config_sha256,
            full_candidate_digest=True,
        )
        if run_id != expected_run_id:
            raise PublishError(
                f"Harbor row {run_id!r}: run_id does not bind rendered agent config"
            )
        if provenance["trial_mapping"] != "openbench_comparison_plan_v3":
            raise PublishError(
                f"Harbor row {run_id!r}: invalid comparison trial mapping"
            )
    else:
        if any(value is not None for value in comparison_values):
            raise PublishError(
                f"Harbor row {run_id!r}: partial comparison identity"
            )
        if (
            provenance["trial_mapping"]
            != "lexicographic_name_within_task_agent_model"
        ):
            raise PublishError(f"Harbor row {run_id!r}: invalid trial_mapping")
        expected_agent_name = expected_harbor_agent_semantic_name(
            provenance["harbor_agent_config_name"]
        )
        if row.get("harness") != expected_agent_name:
            raise PublishError(
                f"Harbor row {run_id!r}: harness does not match immutable "
                "Harbor agent config identity"
            )
        if harbor_model_name != expected_harbor_model_name(
            provenance["harbor_agent_config_name"],
            row.get("model"),
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: canonical model does not match immutable "
                "Harbor model identity"
            )
    if provenance["temporal_matched_block_claim"] is not False:
        raise PublishError(
            f"Harbor row {run_id!r}: temporal_matched_block_claim must be false"
        )
    if provenance["final_workspace_sha256"] is None:
        if workspace is not None:
            raise PublishError(
                f"Harbor row {run_id!r}: workspace_source must be null without "
                "final workspace evidence"
            )
    elif (
        not isinstance(workspace, dict)
        or set(workspace) != {"kind", "sha256"}
        or workspace.get("kind") != "harbor_artifact"
        or workspace.get("sha256") != provenance["final_workspace_sha256"]
    ):
        raise PublishError(
            f"Harbor row {run_id!r}: workspace_source must bind "
            "final_workspace_sha256"
        )
    if terminal_failure:
        expected_class, expected_reason = harbor_exception_semantics(exception_type)
        if (
            row.get("success") is not False
            or row.get("completed") is not False
            or row.get("error") != f"Harbor terminal failure: {exception_type}"
            or row.get("failure_class") != expected_class
            or row.get("failure_reason") != expected_reason
        ):
            raise PublishError(
                f"Harbor row {run_id!r}: terminal failure semantics disagree"
            )
    elif row.get("error") is not None:
        raise PublishError(f"Harbor row {run_id!r}: imported error must be null")
    _validate_harbor_usage(row, provenance)


def _sanitize_harbor_row(row):
    _validate_harbor_row(row)
    return {key: row.get(key) for key in HARBOR_PUBLISH_ROW_KEYS}


def _harbor_import_evidence(rows):
    evidence = []
    for row in rows:
        if not _is_harbor_row(row):
            continue
        _validate_harbor_row(row)
        evidence.append({
            "run_id": row["run_id"],
            "candidate_provenance": dict(row["candidate_provenance"]),
            "usage": {
                "token_basis": row["token_basis"],
                "usage_raw": row["usage_raw"],
            },
            "workspace_source": (
                None
                if row["workspace_source"] is None
                else dict(row["workspace_source"])
            ),
        })
    return sorted(evidence, key=lambda item: item["run_id"])


def _harbor_task_bindings(rows):
    """Return one imported execution binding per task, rejecting conflicts."""
    bindings = {}
    locked_task_digests = {}
    for row in rows:
        if not _is_harbor_row(row):
            continue
        _validate_harbor_row(row)
        task = row.get("task")
        provenance = row["candidate_provenance"]
        locked_digest = provenance["task_digest"].removeprefix("sha256:")
        previous_locked = locked_task_digests.setdefault(task, locked_digest)
        if previous_locked != locked_digest:
            raise PublishError(
                f"Harbor task {task!r}: imported rows disagree on "
                "locked task digest"
            )
        if provenance["openbench_task_content_digest"] is None:
            continue
        binding = {
            "openbench_sha256": provenance["openbench_task_content_digest"][
                "sha256"
            ],
            "harbor_sha256": locked_digest,
            "export": dict(provenance["openbench_harbor_export"]),
        }
        previous = bindings.setdefault(task, binding)
        if previous != binding:
            raise PublishError(
                f"Harbor task {task!r}: imported rows disagree on "
                "execution binding"
            )
    unbound = sorted(set(locked_task_digests) - set(bindings))
    if unbound:
        raise PublishError(
            "Harbor tasks have no verifier-bound OpenBench execution evidence: "
            + ", ".join(repr(task) for task in unbound)
        )
    return bindings


def _harbor_packager_content_hash(task_dir):
    """Reproduce Harbor 0.20.0 Packager.compute_content_hash for an export."""
    task_dir = os.path.abspath(task_dir)
    files = []
    for name in ("task.toml", "instruction.md", "README.md"):
        path = os.path.join(task_dir, name)
        if os.path.isfile(path):
            files.append(path)
    for name in ("environment", "tests", "solution", "steps"):
        root_dir = os.path.join(task_dir, name)
        if not os.path.isdir(root_dir):
            continue
        for root, dirs, names in os.walk(root_dir):
            dirs[:] = sorted(
                directory
                for directory in dirs
                if directory != "__pycache__"
            )
            for filename in sorted(names):
                if (
                    filename == ".DS_Store"
                    or filename.endswith((".pyc", ".swp", ".swo", "~"))
                ):
                    continue
                path = os.path.join(root, filename)
                if os.path.isfile(path):
                    files.append(path)

    outer = hashlib.sha256()
    for path in sorted(files, key=lambda item: os.path.relpath(item, task_dir)):
        rel = os.path.relpath(path, task_dir).replace(os.sep, "/")
        file_hash = _sha256_file(path)
        outer.update(f"{rel}\0{file_hash}\n".encode())
    return outer.hexdigest()


def _canonical_harbor_export_digest(task_dir, task_name, export_config):
    from .export_harbor import ExportError, export_task
    from .workspace import (
        WorkspaceError,
        load_git_workspace_spec,
        resolve_workspace_mode,
    )

    try:
        workspace_mode = resolve_workspace_mode(task_dir)
        if workspace_mode == "git":
            workspace_spec = load_git_workspace_spec(task_dir)
            if workspace_spec.setup:
                raise PublishError(
                    f"Harbor task {task_name!r}: publication cannot safely "
                    "reproduce workspace setup hooks"
                )
            if "://" in workspace_spec.repo or workspace_spec.repo.startswith("git@"):
                raise PublishError(
                    f"Harbor task {task_name!r}: publication cannot safely "
                    "reproduce remote git workspaces"
                )
    except WorkspaceError as exc:
        raise PublishError(
            f"cannot inspect Harbor export workspace for {task_name!r}: {exc}"
        ) from exc

    with tempfile.TemporaryDirectory(prefix="obench-publish-harbor-") as temp_dir:
        export_dir = os.path.join(temp_dir, "task")
        try:
            export_task(
                task_dir,
                export_dir,
                task_name=task_name,
                base_image=export_config["base_image"],
                network_mode=export_config["network_mode"],
            )
        except ExportError as exc:
            raise PublishError(
                f"cannot reproduce Harbor export for {task_name!r}: {exc}"
            ) from exc
        return _harbor_packager_content_hash(export_dir)


def _validate_harbor_task_bindings(rows, tasks_dirs):
    """Require imported execution digests to match publication task trees."""
    roots = stats.parse_tasks_dirs(tasks_dirs)
    for task, binding in _harbor_task_bindings(rows).items():
        task_dir = resolve_task_dir(task, roots)
        if task_dir is None:
            raise PublishError(
                f"Harbor task {task!r}: local publication task tree not found"
            )
        local_digest = task_content_digest(
            task_dir,
            scheme=DIGEST_SCHEME_CURRENT,
        )
        if local_digest != binding["openbench_sha256"]:
            raise PublishError(
                f"Harbor task {task!r}: executed scheme-2 digest "
                f"{binding['openbench_sha256']} does not match local publication task "
                f"{local_digest}"
            )
        canonical_harbor_digest = _canonical_harbor_export_digest(
            task_dir,
            task,
            binding["export"],
        )
        if canonical_harbor_digest != binding["harbor_sha256"]:
            raise PublishError(
                f"Harbor task {task!r}: locked task digest "
                f"{binding['harbor_sha256']} does not match canonical "
                f"OpenBench export {canonical_harbor_digest}"
            )


def sanitize_row_for_publish(row):
    """Strip transcripts and redact absolute local paths from provenance fields."""
    if _is_harbor_row(row):
        return _sanitize_harbor_row(row)
    cleaned = strip_transcript_fields(row)
    prov = cleaned.get("candidate_provenance")
    if isinstance(prov, dict):
        safe = dict(prov)
        for key in ("spec", "config_dir"):
            if key in safe:
                safe[key] = _redact_local_path(safe[key])
        auth_files = safe.get("auth_files")
        if isinstance(auth_files, list):
            safe_auth = []
            for item in auth_files:
                if not isinstance(item, dict):
                    continue
                entry = dict(item)
                if "source" in entry:
                    entry["source"] = _redact_local_path(entry["source"])
                safe_auth.append(entry)
            safe["auth_files"] = safe_auth
        cleaned["candidate_provenance"] = safe
    ws = cleaned.get("workspace_source")
    if isinstance(ws, dict):
        safe_ws = dict(ws)
        if "repo" in safe_ws:
            safe_ws["repo"] = _redact_local_path(safe_ws["repo"])
        cleaned["workspace_source"] = safe_ws
    return cleaned


def rows_reference_transcripts(rows):
    """Return human-readable reasons if any row would pull transcripts into a bundle."""
    problems = []
    for index, row in enumerate(rows, start=1):
        for key in ("transcript_path", "transcript", "transcripts_dir"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                problems.append(
                    f"row {index} ({row.get('run_id', '?')}): field {key!r} "
                    f"points at {value!r} — transcripts are LOCAL-ONLY and "
                    "must never be bundled"
                )
        for key in ("full_output",):
            value = row.get(key)
            if isinstance(value, str) and len(value) > 200:
                problems.append(
                    f"row {index} ({row.get('run_id', '?')}): field {key!r} "
                    "looks like a transcript body — refusing to publish"
                )
    return problems


def find_candidate_gate_record(
        candidate_name, search_dirs=None, *, candidate_digest, model,
        harness_version):
    """Return a live PASS gate bound to the exact candidate arm."""
    roots = list(search_dirs or [])
    if not roots:
        cwd = os.getcwd()
        roots = [
            os.path.join(cwd, "data"),
            os.path.join(cwd, ".openbench", "gate"),
            os.path.join(cwd, "results", "gate"),
        ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith((".json", ".jsonl", ".txt", ".md")):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                candidates_json = []
                try:
                    candidates_json.append(json.loads(text))
                except json.JSONDecodeError:
                    for line in text.splitlines():
                        payload = line.removeprefix("JSON:").strip()
                        if not payload.startswith("{"):
                            continue
                        try:
                            candidates_json.append(json.loads(payload))
                        except json.JSONDecodeError:
                            continue
                for record in candidates_json:
                    if (
                        isinstance(record, dict)
                        and record.get("candidate") == candidate_name
                        and record.get("mode") == "live"
                        and record.get("status") == "PASS"
                        and record.get("pass") is True
                        and record.get("candidate_digest") == candidate_digest
                        and record.get("model") == model
                        and record.get("version") == harness_version
                    ):
                        return path
    return None


def unmatched_arm_warnings(rows):
    """Warn when arms disagree on task set, model, or trial counts."""
    by_arm = defaultdict(list)
    for row in rows:
        if not stats.is_valid_result_row(row):
            continue
        kind = "candidate" if _is_candidate_row(row) else "baseline"
        label = compare._row_arm(row)
        # Mirror report_page / compare labeling for candidate vs stock name clash.
        baseline_names = {
            compare._row_arm(r) for r in rows
            if stats.is_valid_result_row(r) and not _is_candidate_row(r)
        }
        if kind == "candidate" and label in baseline_names:
            display = f"{label} (candidate)"
        else:
            display = label
        by_arm[display].append(row)

    if len(by_arm) < 2:
        return []

    warnings = []
    task_sets = {arm: frozenset(r["task"] for r in arm_rows)
                 for arm, arm_rows in by_arm.items()}
    model_sets = {arm: frozenset(str(r.get("model")) for r in arm_rows)
                  for arm, arm_rows in by_arm.items()}
    trial_maps = {}
    for arm, arm_rows in by_arm.items():
        counts = defaultdict(set)
        for row in arm_rows:
            counts[row["task"]].add(int(row["trial"]))
        trial_maps[arm] = {task: frozenset(trials) for task, trials in counts.items()}

    # Matched-cell coverage (reuse compare unique-cell logic).
    cells_by_arm = {}
    for arm, arm_rows in by_arm.items():
        unique, _dup = compare._unique_cells(arm_rows)
        cells_by_arm[arm] = set(unique)
    common = set.intersection(*cells_by_arm.values()) if cells_by_arm else set()
    for arm, cells in cells_by_arm.items():
        extra = len(cells - common)
        if extra:
            warnings.append(
                f"arm {arm!r} has {extra} (task, trial) cell(s) not shared with "
                f"every other arm (matched denominator would drop them)"
            )

    unique_task_sets = set(task_sets.values())
    if len(unique_task_sets) > 1:
        detail = "; ".join(
            f"{arm}={sorted(tasks)}" for arm, tasks in sorted(task_sets.items())
        )
        warnings.append(f"arms use different task sets: {detail}")

    unique_model_sets = set(model_sets.values())
    if len(unique_model_sets) > 1:
        detail = "; ".join(
            f"{arm}={sorted(models)}" for arm, models in sorted(model_sets.items())
        )
        warnings.append(f"arms use different models: {detail}")

    # Trial-count mismatch on shared tasks.
    shared_tasks = set.intersection(*(set(m) for m in trial_maps.values())) if trial_maps else set()
    for task in sorted(shared_tasks):
        trial_sets = {arm: trial_maps[arm].get(task, frozenset()) for arm in trial_maps}
        if len(set(trial_sets.values())) > 1:
            detail = "; ".join(
                f"{arm}=n_trials={len(trials)}" for arm, trials in sorted(trial_sets.items())
            )
            warnings.append(f"task {task!r} has mismatched trial counts across arms: {detail}")

    return warnings


def gate_missing_warnings(rows, search_dirs=None):
    """Warn when candidate arms lack an archived admission-gate PASS record."""
    warnings = []
    seen = set()
    for row in rows:
        if not _is_candidate_row(row):
            continue
        name = _candidate_name(row)
        provenance = row.get("candidate_provenance") or {}
        candidate_digest = (
            provenance.get("candidate_digest")
            or provenance.get("identity_digest")
        )
        model = row.get("model")
        harness_version = row.get("harness_version")
        identity = (name, candidate_digest, model, harness_version)
        if not name or identity in seen:
            continue
        seen.add(identity)
        if (
            not candidate_digest
            or not model
            or not harness_version
            or find_candidate_gate_record(
                name,
                search_dirs=search_dirs,
                candidate_digest=candidate_digest,
                model=model,
                harness_version=harness_version,
            ) is None
        ):
            warnings.append(
                f"candidate {name!r} digest={candidate_digest!r} "
                f"model={model!r} version={harness_version!r} has no matching "
                "live candidate-gate PASS record under "
                "data/, .openbench/gate/, or results/gate/ — run "
                f"`obench gate <spec> --model ...` (and archive the JSON) "
                "before treating this claim as admission-ready"
            )
    return warnings


def filter_publish_pii(report):
    """Keep only high-signal PII categories (drop digest-like hex/base64 hits)."""
    filtered = {}
    for rel, hits in report.items():
        kept = [hit for hit in hits if hit[0] in PUBLISH_PII_CATEGORIES]
        if kept:
            filtered[rel] = kept
    return filtered


def check_bundle_pii(bundle_dir, ctx=None):
    """Run scrub --check logic on bundle files; return filtered hit report."""
    ctx = ctx or scrub.build_context()
    return filter_publish_pii(scrub.check_tree(bundle_dir, ctx))


def write_results_jsonl(path, rows):
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                      for row in rows)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return payload.encode("utf-8")


def build_provenance(rows, results_sha256, tasks_dirs=None, warnings=None,
                     highlight=None, obench_version=None):
    """Machine-readable provenance for the published bundle."""
    roots = stats.parse_tasks_dirs(tasks_dirs)
    arms = {}
    tasks = {}
    models = sorted({str(row.get("model")) for row in rows})
    trial_counts = defaultdict(set)
    run_dates = []

    for row in rows:
        kind = "candidate" if _is_candidate_row(row) else "baseline"
        name = _arm_name(row)
        key = f"{kind}:{name}"
        if key not in arms:
            entry = {
                "kind": kind,
                "name": name,
                "harness": row.get("harness"),
                "harness_version": row.get("harness_version"),
            }
            if kind == "candidate":
                entry["candidate_provenance"] = row.get("candidate_provenance")
                prov = row.get("candidate_provenance") or {}
                entry["identity_digest"] = (
                    prov.get("candidate_digest") or prov.get("identity_digest")
                )
            else:
                entry["candidate_provenance"] = None
                entry["identity_digest"] = None
            arms[key] = entry

        task = row.get("task")
        if task and task not in tasks:
            task_dir = resolve_task_dir(task, roots)
            digest = None
            if task_dir is not None:
                try:
                    digest = task_content_digest(
                        task_dir, scheme=DIGEST_SCHEME_CURRENT
                    )
                except PublishError:
                    digest = None
            tasks[task] = {
                "task": task,
                "content_digest": digest,
                "workspace_source_samples": [],
            }
        if task and isinstance(row.get("workspace_source"), dict):
            if _is_harbor_row(row):
                sample = dict(row["workspace_source"])
            else:
                sample = sanitize_row_for_publish(
                    {"workspace_source": row["workspace_source"]}
                )["workspace_source"]
            if sample not in tasks[task]["workspace_source_samples"]:
                tasks[task]["workspace_source_samples"].append(sample)

        trial_counts[(row.get("harness"), row.get("task"), row.get("model"))].add(
            int(row["trial"]) if row.get("trial") is not None else 0
        )
        for field in ("started_at", "finished_at", "timestamp", "run_at"):
            value = row.get(field)
            if isinstance(value, str) and value:
                run_dates.append(value)

    trial_summary = {
        f"{h}/{t}/{m}": sorted(trials)
        for (h, t, m), trials in sorted(trial_counts.items())
    }
    provenance = {
        "obench_version": obench_version or __version__,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "results_sha256": results_sha256,
        "digest_scheme": DIGEST_SCHEME_CURRENT,
        "arms": [arms[key] for key in sorted(arms)],
        "tasks": [tasks[name] for name in sorted(tasks)],
        "models": models,
        "trial_counts": trial_summary,
        "run_dates": sorted(set(run_dates)),
        "highlight_arms": list(highlight or []),
        "warnings": list(warnings or []),
    }
    harbor_evidence = _harbor_import_evidence(rows)
    if harbor_evidence:
        provenance["harbor_import_evidence"] = harbor_evidence
    return provenance


def default_out_dir(name=None):
    stamp = name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stamp).strip("-") or "bundle"
    return os.path.join(os.getcwd(), "openbench-publish", safe)


def render_publish_card(models, *, title, headline, highlight_arms, warnings,
                        methodology, meta):
    """Self-contained comparison card built on report_page helpers."""
    # Prefer report_page.render_page (charts + highlighting) then inject a
    # publish-specific headline metrics table with mean score / mean wall.
    page = report_page.render_page(
        models,
        methodology,
        title=title,
        headline=headline,
        highlight_arms=highlight_arms,
        banner_warnings=warnings,
    )
    # Insert a compact metrics table after the header for the show-off card.
    metrics = _publish_metrics_section(models, highlight_arms, meta)
    marker = "</header>"
    if marker in page:
        page = page.replace(marker, marker + metrics, 1)
    return page


def _publish_metrics_section(models, highlight_arms, meta):
    highlight = {str(name) for name in (highlight_arms or [])}
    heads = ["Arm", "Solved/n", "Solve rate", "Wilson 95% CI", "Mean score",
             "Mean wall", "Tokens/solve", "Token basis"]
    rows_html = []
    for model in models:
        for arm in model["arms"]:
            wilson = arm.get("wilson") or (None, None)
            if wilson[0] is None:
                wilson_s = "—"
            else:
                wilson_s = f"{wilson[0] * 100:.1f}–{wilson[1] * 100:.1f}%"
            basis = arm.get("token_basis") or "—"
            values = [
                f"{arm['arm']} × {model['model']}",
                f"{arm['solved']}/{arm['n']}",
                report_page._pct(arm.get("rate")),
                wilson_s,
                report_page._num(arm.get("mean_score"), 3),
                (report_page._num(arm.get("mean_wall"), 1) + "s"
                 if arm.get("mean_wall") is not None else "—"),
                report_page._num(arm.get("total_tokens")),
                basis,
            ]
            css = ' class="highlight"' if arm["arm"] in highlight else ""
            cells = "".join(f"<td>{html.escape(str(v))}</td>" for v in values)
            rows_html.append(f"<tr{css} data-arm=\"{html.escape(arm['arm'], quote=True)}\">"
                             f"{cells}</tr>")
    head = "".join(f"<th>{html.escape(h)}</th>" for h in heads)
    meta_bits = [
        f"obench {html.escape(str(meta.get('obench_version', '')))}",
        f"tasks: {html.escape(', '.join(meta.get('tasks') or []) or '—')}",
        f"models: {html.escape(', '.join(meta.get('models') or []) or '—')}",
        f"trials: {html.escape(str(meta.get('trial_summary') or '—'))}",
    ]
    if meta.get("run_dates"):
        meta_bits.append("dates: " + html.escape(", ".join(meta["run_dates"])))
    basis_note = (
        '<p class="table-note token-note">Tokens/solve uses fresh totals: '
        'self-reported <code>tokens</code> when present, else proxy-measured '
        '(uncached input + output). Badge: unmetered / self-reported / '
        'proxy-measured. Cache-read is not folded into Tokens/solve.</p>'
    )
    return (
        '<section class="publish-card"><h2>Comparison card</h2>'
        '<p class="tag">' + " · ".join(meta_bits) + "</p>"
        '<div class="scroll"><table class="results"><thead><tr>'
        + head + "</tr></thead><tbody>" + "".join(rows_html)
        + "</tbody></table></div>" + basis_note + "</section>"
    )


def bundle_readme(provenance):
    warnings = provenance.get("warnings") or []
    warning_block = ""
    if warnings:
        warning_block = "\n## Comparability warnings\n\n" + "\n".join(
            f"- {w}" for w in warnings
        ) + "\n"
    return f"""# OpenBench publish bundle

This directory is a **shareable comparison artifact** produced by
`obench publish`. It is meant to be posted as evidence that a candidate
harness was run against OpenBench stock arms.

## Contents

| File | Purpose |
|------|---------|
| `index.html` | Self-contained HTML comparison card (candidate arms highlighted) |
| `results.jsonl` | Filtered result rows included in the claim (no transcripts) |
| `provenance.json` | Machine-readable digests + arm identities + results SHA-256 |
| `README.md` | This file |

## Re-verify

```bash
obench verify .
```

`obench verify` recomputes the SHA-256 of `results.jsonl` and, when task
trees are available locally, per-task content digests recorded in
`provenance.json`. Each check prints PASS or FAIL.

### What verify proves

- The results file has not been edited since publish (hash match).
- Task content digests still match the recorded instruction/checker/workspace
  hashes when those tasks are present on the verifying machine.

### What verify does NOT prove

- The runs were not cherry-picked or rerun until green.
- Live candidate-gate checks succeeded on the publisher's machine.
- Transcripts were reviewed (they are **never** included — LOCAL-ONLY).

Publish the full matrix whenever possible.
{warning_block}
Generated by obench {provenance.get("obench_version", "?")} at
{provenance.get("generated_at", "?")}.
"""


def create_bundle(results_path, out_dir, *, candidate_specs=None, tasks_dirs=None,
                  title=None, allow_pii_override=False, gate_search_dirs=None,
                  scrub_ctx=None, allow_incomplete=False):
    """Build a publish bundle under ``out_dir``. Returns provenance dict."""
    results_path = os.path.abspath(results_path)
    if not os.path.isfile(results_path):
        raise PublishError(f"results not found: {results_path}")

    rows = stats.load_rows([results_path])
    if not rows:
        raise PublishError(f"no result rows in {results_path}")
    invalid = sum(not stats.is_valid_result_row(row) for row in rows)
    if invalid:
        raise PublishError(
            f"{results_path}: {invalid} invalid result row(s); refusing to publish"
        )
    try:
        stats.validate_suite_rows(rows, for_publication=True)
    except ValueError as exc:
        raise PublishError(str(exc)) from exc

    transcript_problems = rows_reference_transcripts(rows)
    if transcript_problems:
        raise PublishError(
            "refusing to publish: transcript material would be bundled:\n  "
            + "\n  ".join(transcript_problems)
        )

    # Never copy a sibling transcripts/ tree into the out dir.
    sibling_transcripts = os.path.join(
        os.path.dirname(results_path), "transcripts"
    )
    if os.path.isdir(sibling_transcripts) and os.path.abspath(out_dir).startswith(
            os.path.abspath(sibling_transcripts) + os.sep):
        raise PublishError("refusing to publish into a transcripts/ directory")

    highlight = resolve_highlight_names(candidate_specs)
    # Auto-highlight every candidate arm when none named.
    if not highlight:
        highlight = sorted({
            _candidate_name(row) for row in rows
            if _candidate_name(row)
        })

    publish_rows = [sanitize_row_for_publish(row) for row in rows]
    try:
        warnings = unmatched_arm_warnings(publish_rows)
    except ValueError as exc:
        raise PublishError(str(exc)) from exc
    warnings.extend(gate_missing_warnings(publish_rows, search_dirs=gate_search_dirs))
    if warnings and not allow_incomplete:
        raise PublishError(
            "comparison is incomplete or lacks live candidate admission evidence:\n  "
            + "\n  ".join(warnings)
            + "\nRe-run with --allow-incomplete only when publishing an "
              "explicitly caveated artifact."
        )

    if tasks_dirs is None:
        discovered = default_tasks_dir()
        tasks_dirs = [discovered] if discovered else []

    _validate_harbor_task_bindings(publish_rows, tasks_dirs)

    os.makedirs(out_dir, exist_ok=True)
    # Refuse if caller somehow pointed --out at transcripts.
    if os.path.basename(os.path.abspath(out_dir)) == "transcripts":
        raise PublishError("refusing to use transcripts/ as --out")

    results_out = os.path.join(out_dir, "results.jsonl")
    raw = write_results_jsonl(results_out, publish_rows)
    results_sha = _sha256_bytes(raw)

    provenance = build_provenance(
        publish_rows,
        results_sha,
        tasks_dirs=tasks_dirs,
        warnings=warnings,
        highlight=highlight,
        obench_version=__version__,
    )
    provenance_path = os.path.join(out_dir, "provenance.json")
    with open(provenance_path, "w", encoding="utf-8") as fh:
        json.dump(provenance, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")

    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(bundle_readme(provenance))

    # Assemble HTML via report_page machinery.
    tmp_results = results_out
    models = report_page.assemble_tables(
        [{"path": tmp_results, "matched": False}],
        tasks_dirs=tasks_dirs or [os.getcwd()],
    )
    task_names = sorted({row["task"] for row in publish_rows})
    trial_ns = sorted({
        len(v) for v in provenance.get("trial_counts", {}).values()
    }) or ["?"]
    meta = {
        "obench_version": __version__,
        "tasks": task_names,
        "models": provenance.get("models") or [],
        "trial_summary": (
            f"{min(trial_ns)}–{max(trial_ns)}" if len(trial_ns) > 1
            else str(trial_ns[0])
        ),
        "run_dates": provenance.get("run_dates") or [],
    }
    # Map highlight names onto report_page arm labels (may include " (candidate)").
    arm_labels = set()
    for model in models:
        for arm in model["arms"]:
            arm_labels.add(arm["arm"])
    highlight_labels = []
    for name in highlight:
        if name in arm_labels:
            highlight_labels.append(name)
        elif f"{name} (candidate)" in arm_labels:
            highlight_labels.append(f"{name} (candidate)")
        else:
            # Partial match: any label that starts with the candidate name.
            highlight_labels.extend(
                label for label in arm_labels
                if label == name or label.startswith(name + " ")
            )

    card_title = title or "OpenBench comparison"
    headline = (
        f"Candidate arm(s) highlighted vs stock · {len(publish_rows)} rows · "
        f"obench {__version__}"
    )
    page = render_publish_card(
        models,
        title=card_title,
        headline=headline,
        highlight_arms=highlight_labels,
        warnings=warnings,
        methodology=PUBLISH_METHODOLOGY,
        meta=meta,
    )
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)

    # Safety: ensure we did not create a transcripts/ directory in the bundle.
    if os.path.isdir(os.path.join(out_dir, "transcripts")):
        shutil.rmtree(os.path.join(out_dir, "transcripts"), ignore_errors=True)
        raise PublishError("internal error: transcripts/ appeared in the bundle")

    pii_report = check_bundle_pii(out_dir, ctx=scrub_ctx)
    if pii_report:
        total = sum(len(v) for v in pii_report.values())
        scrub._print_check_report(out_dir, pii_report)
        message = (
            f"PII sanity check found {total} potential hit(s) in the bundle. "
            "Refuse to publish by default. Re-run with --allow-pii-override "
            "only after manual review (dangerous)."
        )
        if not allow_pii_override:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise PublishError(message)
        print(f"WARNING: {message}", file=sys.stderr)
        print("(proceeding because --allow-pii-override was set)", file=sys.stderr)

    return provenance


def verify_bundle(bundle_dir, tasks_dirs=None, *, verify_task_trees=True):
    """Recompute digests; return list of {name, status, detail} checks."""
    bundle_dir = os.path.abspath(bundle_dir)
    checks = []
    provenance_path = os.path.join(bundle_dir, "provenance.json")
    results_path = os.path.join(bundle_dir, "results.jsonl")

    if not os.path.isfile(provenance_path):
        return [{"name": "provenance.json", "status": "FAIL",
                 "detail": "missing provenance.json"}]
    if not os.path.isfile(results_path):
        return [{"name": "results.jsonl", "status": "FAIL",
                 "detail": "missing results.jsonl"}]

    try:
        with open(provenance_path, encoding="utf-8") as fh:
            provenance = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return [{"name": "provenance.json", "status": "FAIL",
                 "detail": f"invalid provenance.json: {exc}"}]
    if not isinstance(provenance, dict):
        return [{"name": "provenance.json", "status": "FAIL",
                 "detail": "provenance.json must contain an object"}]

    expected = provenance.get("results_sha256")
    actual = _sha256_file(results_path)
    checks.append({
        "name": "results_sha256",
        "status": "PASS" if expected and expected == actual else "FAIL",
        "detail": f"expected={expected} actual={actual}",
    })

    rows = []
    row_error = None
    try:
        with open(results_path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict) or not stats.is_valid_result_row(value):
                    raise ValueError(f"line {lineno} is not a valid result row")
                rows.append(value)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        row_error = str(exc)
    checks.append({
        "name": "results_rows",
        "status": "FAIL" if row_error or not rows else "PASS",
        "detail": row_error or f"{len(rows)} valid result row(s)",
    })
    if rows:
        try:
            stats.validate_suite_rows(rows, for_publication=True)
            suite_policy_error = None
        except ValueError as exc:
            suite_policy_error = str(exc)
        if any(
            isinstance(row.get("candidate_provenance"), dict)
            and any(
                key.startswith("suite_")
                for key in row["candidate_provenance"]
            )
            for row in rows
        ):
            checks.append({
                "name": "suite_publication_policy",
                "status": "FAIL" if suite_policy_error else "PASS",
                "detail": suite_policy_error or "public complete suite",
            })

    try:
        expected_harbor_evidence = _harbor_import_evidence(rows)
        harbor_evidence_error = None
    except PublishError as exc:
        expected_harbor_evidence = []
        harbor_evidence_error = str(exc)
    declared_harbor_evidence = provenance.get("harbor_import_evidence")
    if (
        harbor_evidence_error is not None
        or expected_harbor_evidence
        or declared_harbor_evidence is not None
    ):
        harbor_evidence_ok = (
            harbor_evidence_error is None
            and isinstance(declared_harbor_evidence, list)
            and declared_harbor_evidence == expected_harbor_evidence
        )
        checks.append({
            "name": "harbor_import_evidence",
            "status": "PASS" if harbor_evidence_ok else "FAIL",
            "detail": (
                harbor_evidence_error
                or f"declared={len(declared_harbor_evidence) if isinstance(declared_harbor_evidence, list) else 'invalid'} "
                   f"rows={len(expected_harbor_evidence)}"
            ),
        })

    declared_tasks = provenance.get("tasks")
    task_entries = declared_tasks if isinstance(declared_tasks, list) else []
    declared_names = {
        item.get("task") for item in task_entries
        if isinstance(item, dict) and isinstance(item.get("task"), str)
    }
    duplicate_names = sorted({
        name for name in declared_names
        if sum(
            1 for item in task_entries
            if isinstance(item, dict) and item.get("task") == name
        ) > 1
    })
    row_tasks = {
        row.get("task") for row in rows if isinstance(row.get("task"), str)
    }
    manifest_ok = (
        isinstance(declared_tasks, list)
        and not duplicate_names
        and declared_names == row_tasks
        and all(
            isinstance(item, dict)
            and isinstance(item.get("task"), str)
            and bool(item["task"])
            and isinstance(item.get("content_digest"), str)
            and re.fullmatch(r"[0-9a-f]{64}", item["content_digest"]) is not None
            for item in task_entries
        )
    )
    checks.append({
        "name": "task_manifest",
        "status": "PASS" if manifest_ok else "FAIL",
        "detail": (
            f"declared={sorted(declared_names)} rows={sorted(row_tasks)} "
            f"duplicates={duplicate_names}"
            if isinstance(declared_tasks, list)
            else "provenance tasks must be a list"
        ),
    })

    try:
        digest_scheme = resolve_digest_scheme(provenance)
    except PublishError as exc:
        checks.append({
            "name": "digest_scheme",
            "status": "FAIL",
            "detail": str(exc),
        })
        return checks

    try:
        harbor_task_bindings = _harbor_task_bindings(rows)
        harbor_binding_error = None
    except PublishError as exc:
        harbor_task_bindings = {}
        harbor_binding_error = str(exc)
    if harbor_binding_error is not None:
        checks.append({
            "name": "harbor_task_binding",
            "status": "FAIL",
            "detail": harbor_binding_error,
        })
    for task, binding in sorted(harbor_task_bindings.items()):
        matching_entries = [
            item for item in task_entries
            if isinstance(item, dict) and item.get("task") == task
        ]
        recorded_digest = (
            matching_entries[0].get("content_digest")
            if len(matching_entries) == 1
            else None
        )
        binding_ok = (
            digest_scheme == DIGEST_SCHEME_CURRENT
            and recorded_digest == binding["openbench_sha256"]
        )
        checks.append({
            "name": f"harbor_task_binding:{task}",
            "status": "PASS" if binding_ok else "FAIL",
            "detail": (
                f"scheme={digest_scheme} "
                f"executed={binding['openbench_sha256']} "
                f"published={recorded_digest}"
            ),
        })

    if tasks_dirs is None:
        discovered = default_tasks_dir()
        tasks_dirs = [discovered] if discovered else []
    roots = stats.parse_tasks_dirs(tasks_dirs) if tasks_dirs else []

    for task_entry in task_entries:
        if not isinstance(task_entry, dict):
            continue
        task = task_entry.get("task")
        expected_digest = task_entry.get("content_digest")
        name = f"task_digest:{task}"
        if not expected_digest:
            checks.append({
                "name": name,
                "status": "FAIL",
                "detail": "no content_digest recorded at publish time; "
                          "cannot verify task fingerprint",
            })
            continue
        if not verify_task_trees:
            continue
        task_dir = resolve_task_dir(task, roots) if roots else None
        if task_dir is None:
            checks.append({
                "name": name,
                "status": "FAIL",
                "detail": f"task tree not found for {task!r}; cannot recompute digest",
            })
            continue
        try:
            actual_digest = task_content_digest(task_dir, scheme=digest_scheme)
        except PublishError as exc:
            checks.append({"name": name, "status": "FAIL", "detail": str(exc)})
            continue
        checks.append({
            "name": name,
            "status": "PASS" if actual_digest == expected_digest else "FAIL",
            "detail": (
                f"scheme={digest_scheme} expected={expected_digest} "
                f"actual={actual_digest}"
            ),
        })
        binding = harbor_task_bindings.get(task)
        if binding is not None:
            try:
                canonical_harbor_digest = _canonical_harbor_export_digest(
                    task_dir,
                    task,
                    binding["export"],
                )
                export_binding_error = None
            except (OSError, PublishError) as exc:
                canonical_harbor_digest = None
                export_binding_error = str(exc)
            checks.append({
                "name": f"harbor_export_binding:{task}",
                "status": (
                    "PASS"
                    if (
                        export_binding_error is None
                        and canonical_harbor_digest == binding["harbor_sha256"]
                    )
                    else "FAIL"
                ),
                "detail": (
                    export_binding_error
                    or f"locked={binding['harbor_sha256']} "
                    f"canonical={canonical_harbor_digest}"
                ),
            })

    return checks


def print_verify_report(checks):
    failed = 0
    for item in checks:
        print(f"{item['status']}: {item['name']} — {item['detail']}")
        if item["status"] != "PASS":
            failed += 1
    verdict = "FAIL" if failed else "PASS"
    print(f"VERDICT: {verdict} ({len(checks) - failed}/{len(checks)} checks passed)")
    return 0 if failed == 0 else 1


def _publish_main(argv):
    parser = argparse.ArgumentParser(
        prog="obench publish",
        description="Build a shareable comparison bundle (HTML + provenance).",
    )
    parser.add_argument(
        "--results-path", default=None,
        help="results JSONL (default: same resolution as run/report)",
    )
    parser.add_argument(
        "--candidate", action="append", default=[],
        help="candidate name or spec path to highlight (repeatable)",
    )
    parser.add_argument(
        "--out", default=None,
        help="output bundle directory (default: ./openbench-publish/<timestamp>/)",
    )
    parser.add_argument(
        "--name", default=None,
        help="bundle directory name under openbench-publish/ (ignored if --out set)",
    )
    parser.add_argument(
        "--tasks-dir", action="append", default=None,
        help="task root for content digests (repeatable)",
    )
    parser.add_argument("--title", default=None, help="HTML card title")
    parser.add_argument(
        "--allow-pii-override", action="store_true",
        help="DANGEROUS: publish even when the PII sanity check finds hits",
    )
    parser.add_argument(
        "--allow-incomplete", action="store_true",
        help="publish despite unmatched cells or missing live candidate gate evidence",
    )
    args = parser.parse_args(argv)

    results_path = args.results_path
    if results_path is None:
        from .config import load_config
        cfg = load_config()
        results_path = cfg.results_path or default_results_path()

    out_dir = os.path.abspath(args.out) if args.out else default_out_dir(args.name)
    tasks_dirs = args.tasks_dir
    if not tasks_dirs:
        try:
            tasks_dirs = [resolve_tasks_dir()]
        except Exception:  # noqa: BLE001 - publish can proceed without tasks
            tasks_dirs = []

    try:
        provenance = create_bundle(
            results_path,
            out_dir,
            candidate_specs=args.candidate,
            tasks_dirs=tasks_dirs,
            title=args.title,
            allow_pii_override=args.allow_pii_override,
            allow_incomplete=args.allow_incomplete,
        )
    except PublishError as exc:
        print(f"publish: {exc}", file=sys.stderr)
        return 2

    for warning in provenance.get("warnings") or []:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"Published bundle → {out_dir}")
    print(f"  index.html      comparison card")
    print(f"  results.jsonl   {len(stats.load_rows([os.path.join(out_dir, 'results.jsonl')]))} rows")
    print(f"  provenance.json results_sha256={provenance['results_sha256'][:16]}…")
    print(f"  README.md       re-verify with: obench verify {out_dir}")
    return 0


def _verify_main(argv):
    parser = argparse.ArgumentParser(
        prog="obench verify",
        description="Re-verify an OpenBench publish bundle's digests.",
    )
    parser.add_argument("bundle_dir", help="path to a publish bundle directory")
    parser.add_argument(
        "--tasks-dir", action="append", default=None,
        help="task root for recomputing content digests (repeatable)",
    )
    args = parser.parse_args(argv)

    tasks_dirs = args.tasks_dir
    if not tasks_dirs:
        try:
            tasks_dirs = [resolve_tasks_dir()]
        except Exception:  # noqa: BLE001
            tasks_dirs = []

    checks = verify_bundle(args.bundle_dir, tasks_dirs=tasks_dirs)
    return print_verify_report(checks)


def main(argv=None):
    """Entry used when invoked as ``python -m obench.publish`` (publish only)."""
    return _publish_main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
