"""Per-trial CountingProxy evidence for local Harbor Codex runs."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from obench import proxy
from obench.run import proxy_split_from_usage

SCHEMA_VERSION = "openbench.harbor-metering.v1"
HARBOR_BASE_URL_ENV = "OPENAI_BASE_URL"
HARBOR_BASE_URL_SOURCE_ENV = "OPENBENCH_HARBOR_METERING_BASE_URL"
STATUSES = {"exact", "mismatch", "incomplete"}
COUNTER_FIELDS = ("calls", "input_tokens", "cache_tokens", "output_tokens")


class HarborMeteringError(RuntimeError):
    """Raised when Harbor metering cannot be configured or verified."""


class HarborMeteringPublicationError(HarborMeteringError):
    """Raised when required proxy evidence is not publication-grade."""


@dataclass(frozen=True)
class UsageCounters:
    """Comparable totals; input includes cached input, as Harbor reports it."""

    calls: int | None
    input_tokens: int | None
    cache_tokens: int | None
    output_tokens: int | None

    def __post_init__(self) -> None:
        for field in COUNTER_FIELDS:
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field} must be a nonnegative integer or None")
        if (
            self.input_tokens is not None
            and self.cache_tokens is not None
            and self.cache_tokens > self.input_tokens
        ):
            raise ValueError("cache_tokens cannot exceed input_tokens")

    @classmethod
    def from_openbench_row(cls, row: Mapping[str, Any]) -> "UsageCounters":
        """Map row token totals; call count must come from the ATIF trajectory."""
        uncached = _optional_int(row.get("tokens_input_uncached"))
        cache = _optional_int(row.get("tokens_cache_read"))
        total_input = None if uncached is None or cache is None else uncached + cache
        return cls(
            calls=None,
            input_tokens=total_input,
            cache_tokens=cache,
            output_tokens=_optional_int(row.get("tokens_output")),
        )

    @classmethod
    def from_atif_trajectory(cls, trajectory: Any) -> "UsageCounters":
        """Use ATIF-v1.7 ``llm_call_count`` and final token metrics."""
        if trajectory is None:
            return cls(None, None, None, None)
        steps = _member(trajectory, "steps")
        if not isinstance(steps, (list, tuple)):
            calls = None
        else:
            counts = []
            calls_complete = True
            for step in steps:
                if _member(step, "source") != "agent":
                    continue
                count = _optional_int(_member(step, "llm_call_count"))
                if count is None:
                    calls_complete = False
                    break
                counts.append(count)
            calls = sum(counts) if calls_complete else None

        metrics = _member(trajectory, "final_metrics")
        return cls(
            calls=calls,
            input_tokens=_optional_int(_member(metrics, "total_prompt_tokens")),
            cache_tokens=_optional_int(_member(metrics, "total_cached_tokens")),
            output_tokens=_optional_int(
                _member(metrics, "total_completion_tokens")
            ),
        )


@dataclass(frozen=True)
class Reconciliation:
    status: str
    fields: dict[str, dict[str, Any]]


def reconcile_usage(
    agent_reported: UsageCounters,
    proxy_measured: UsageCounters,
    *,
    proxy_complete: bool,
) -> Reconciliation:
    """Classify complete equal totals, complete unequal totals, or missing evidence."""
    fields: dict[str, dict[str, Any]] = {}
    any_incomplete = not proxy_complete
    any_mismatch = False
    for name in COUNTER_FIELDS:
        agent_value = getattr(agent_reported, name)
        proxy_value = getattr(proxy_measured, name)
        if not proxy_complete or agent_value is None or proxy_value is None:
            state = "incomplete"
            any_incomplete = True
        elif agent_value != proxy_value:
            state = "mismatch"
            any_mismatch = True
        else:
            state = "exact"
        fields[name] = {
            "agent_reported": agent_value,
            "proxy_measured": proxy_value,
            "status": state,
        }
    status = "incomplete" if any_incomplete else ("mismatch" if any_mismatch else "exact")
    return Reconciliation(status=status, fields=fields)


def publication_decision(
    evidence: Mapping[str, Any], *, proxy_required: bool
) -> dict[str, Any]:
    """Return the fail-closed importer/publication decision for one trial."""
    status = evidence.get("reconciliation", {}).get("status")
    valid_status = status in STATUSES
    eligible = not proxy_required or (valid_status and status == "exact")
    reasons = []
    if proxy_required and not valid_status:
        reasons.append("invalid_metering_evidence")
    elif proxy_required and status != "exact":
        reasons.append(f"proxy_evidence_{status}")
    return {
        "proxy_evidence_required": proxy_required,
        "eligible": bool(eligible),
        "blocking_reasons": reasons,
    }


def require_publication_eligible(
    evidence: Mapping[str, Any], *, proxy_required: bool
) -> None:
    """Raise unless the supplied evidence satisfies the publication policy."""
    decision = publication_decision(evidence, proxy_required=proxy_required)
    if not decision["eligible"]:
        reasons = ", ".join(decision["blocking_reasons"])
        raise HarborMeteringPublicationError(
            f"Harbor trial is blocked from publication: {reasons}"
        )


def apply_to_imported_row(
    row: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    proxy_required: bool,
) -> dict[str, Any]:
    """Integrator hook: attach measured totals and a machine publication gate."""
    updated = dict(row)
    proxy_usage = evidence.get("proxy_measured")
    if not isinstance(proxy_usage, Mapping):
        proxy_usage = {}
    total_input = _optional_int(proxy_usage.get("input_tokens"))
    cache = _optional_int(proxy_usage.get("cache_tokens"))
    updated.update(
        {
            "tokens_proxy_calls": _optional_int(proxy_usage.get("calls")),
            "tokens_proxy_input_uncached": (
                None
                if total_input is None or cache is None
                else max(0, total_input - cache)
            ),
            "tokens_proxy_cache_read": cache,
            "tokens_proxy_output": _optional_int(proxy_usage.get("output_tokens")),
            "token_basis_proxy": (
                "proxy_measured"
                if evidence.get("proxy_complete") is True
                else None
            ),
        }
    )
    provenance = dict(updated.get("candidate_provenance") or {})
    provenance["harbor_metering"] = {
        "schema_version": evidence.get("schema_version"),
        "reconciliation_status": evidence.get("reconciliation", {}).get("status"),
        "ledger_root_hash": evidence.get("ledger_seal", {}).get("root_hash"),
        "publication": publication_decision(
            evidence, proxy_required=proxy_required
        ),
    }
    provenance["proxy_measured"] = evidence.get("proxy_complete") is True
    updated["candidate_provenance"] = provenance
    return updated


def load_evidence(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a sealed evidence artifact with structural fail-closed checks."""
    requested = Path(path)
    if requested.is_symlink() or not requested.is_file():
        raise HarborMeteringError(f"metering evidence is not a regular file: {requested}")
    try:
        value = json.loads(requested.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborMeteringError(f"cannot read metering evidence: {requested}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise HarborMeteringError("unsupported Harbor metering evidence schema")
    reconciliation = value.get("reconciliation")
    if (
        not isinstance(reconciliation, dict)
        or reconciliation.get("status") not in STATUSES
    ):
        raise HarborMeteringError("invalid Harbor metering reconciliation")
    return value


class HarborMeteringSession:
    """Own one Docker-reachable proxy route and its private per-trial ledger."""

    def __init__(
        self,
        evidence_dir: str | os.PathLike[str],
        trial_id: str,
        *,
        listen_host: str = "0.0.0.0",
        advertised_host: str = "host.docker.internal",
        port: int = 0,
    ) -> None:
        if not isinstance(trial_id, str) or not trial_id.strip() or "\0" in trial_id:
            raise ValueError("trial_id must be a nonempty string without NUL bytes")
        if not advertised_host or "/" in advertised_host:
            raise ValueError("advertised_host must be a hostname or address")

        self.trial_id = trial_id
        self.evidence_dir = Path(evidence_dir).expanduser().resolve()
        self.private_dir = self.evidence_dir / "private"
        self.evidence_path = self.evidence_dir / "harbor-metering.json"
        self._token = proxy.new_cell_token()
        self._server = None
        self._thread = None
        self._sealed = False
        self._closed = False

        _mkdir_private(self.evidence_dir)
        _mkdir_private(self.private_dir)
        server = proxy.make_server(
            listen_host,
            port,
            self.private_dir,
            require_registered_tokens=True,
        )
        try:
            server.register_cell(self._token)
            _write_private_json(
                self.private_dir / f"{self._token}.meta.json",
                {
                    "source": "harbor_metering",
                    "harness": "codex",
                    "trial_id_sha256": hashlib.sha256(
                        trial_id.encode("utf-8")
                    ).hexdigest(),
                },
            )
            thread = threading.Thread(
                target=server.serve_forever,
                name="openbench-harbor-counting-proxy",
                daemon=True,
            )
            thread.start()
        except BaseException:
            server.server_close()
            raise
        self._server = server
        self._thread = thread
        actual_port = int(server.server_address[1])
        host = f"[{advertised_host}]" if ":" in advertised_host else advertised_host
        self._base_url = (
            f"http://{host}:{actual_port}/cell/{self._token}/"
            "codex/backend-api/codex"
        )

    @property
    def agent_env(self) -> dict[str, str]:
        """Harbor ``--ae`` value; the persisted value is a non-secret template."""
        if self._closed:
            raise HarborMeteringError("metering session is closed")
        return {
            HARBOR_BASE_URL_ENV: f"${{{HARBOR_BASE_URL_SOURCE_ENV}}}",
        }

    def process_env(
        self, base: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Environment for the Harbor host process; contains the ephemeral route."""
        if self._closed:
            raise HarborMeteringError("metering session is closed")
        environment = dict(os.environ if base is None else base)
        environment[HARBOR_BASE_URL_SOURCE_ENV] = self._base_url
        return environment

    @property
    def runtime_base_url(self) -> str:
        """Ephemeral endpoint for an in-process Harbor agent integration."""
        if self._closed:
            raise HarborMeteringError("metering session is closed")
        return self._base_url

    @property
    def server(self):
        """The owned CountingProxy server, exposed for lifecycle integration/tests."""
        return self._server

    def seal(
        self,
        agent_reported: UsageCounters,
        *,
        proxy_required: bool = True,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        """Drain, verify, reconcile, and atomically persist one trial's evidence."""
        if self._closed:
            raise HarborMeteringError("metering session is closed")
        if self._sealed:
            return load_evidence(self.evidence_path)

        errors: list[str] = []
        seal_data: dict[str, Any] | None = None
        proxy_usage = UsageCounters(None, None, None, None)
        proxy_complete = False
        try:
            seal = self._server.seal_cell(self._token, timeout_s=timeout_s)
            os.chmod(seal.path, 0o600)
            records, seal_record = _verified_ledger(seal.path)
            seal_data = {
                "record_count": seal.record_count,
                "last_sequence": seal.last_sequence,
                "root_hash": seal.root_hash,
                "ledger_sha256": _sha256_file(seal.path),
            }
            if (
                seal_record["record_count"] != seal.record_count
                or seal_record["last_sequence"] != seal.last_sequence
                or seal_record["root_hash"] != seal.root_hash
            ):
                raise HarborMeteringError("in-memory and durable ledger seals differ")
            proxy_usage, usage_errors = _proxy_totals(records)
            errors.extend(usage_errors)
            proxy_complete = not errors
        except Exception as exc:
            errors.append(f"ledger_finalization_failed:{type(exc).__name__}")

        reconciliation = reconcile_usage(
            agent_reported, proxy_usage, proxy_complete=proxy_complete
        )
        evidence: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "trial_id": self.trial_id,
            "transport": {
                "client_to_proxy": "http",
                "proxy_to_upstream": "https",
                "tls_interception": False,
                "upstream_origin": "https://chatgpt.com",
                "route": "codex/backend-api/codex",
            },
            "agent_reported": asdict(agent_reported),
            "proxy_measured": asdict(proxy_usage),
            "proxy_complete": proxy_complete,
            "ledger_seal": seal_data,
            "reconciliation": {
                "status": reconciliation.status,
                "fields": reconciliation.fields,
            },
            "errors": errors,
        }
        evidence["publication"] = publication_decision(
            evidence, proxy_required=proxy_required
        )
        _write_private_json(self.evidence_path, evidence)
        self._sealed = True
        return evidence

    def close(self) -> None:
        """Stop accepting traffic and synchronously release the listener."""
        if self._closed:
            return
        self._closed = True
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                raise HarborMeteringError("CountingProxy thread did not stop")

    def __enter__(self) -> "HarborMeteringSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _verified_ledger(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise HarborMeteringError("cannot read proxy ledger") from exc
    if not lines:
        raise HarborMeteringError("proxy ledger is empty")
    try:
        values = [json.loads(line) for line in lines]
    except json.JSONDecodeError as exc:
        raise HarborMeteringError("proxy ledger is not valid JSONL") from exc
    if not all(isinstance(value, dict) for value in values):
        raise HarborMeteringError("proxy ledger records must be JSON objects")
    seal = values[-1]
    if seal.get("record_type") != "ledger_seal" or seal.get("state") != "SEALED":
        raise HarborMeteringError("proxy ledger lacks a terminal seal")

    requests = values[:-1]
    previous_hash = proxy.EMPTY_LEDGER_HASH
    for sequence, record in enumerate(requests, 1):
        if record.get("record_type") != "request":
            raise HarborMeteringError("proxy ledger contains a non-request record")
        if record.get("sequence") != sequence or record.get("previous_hash") != previous_hash:
            raise HarborMeteringError("proxy ledger hash-chain position is invalid")
        chained = dict(record)
        recorded_hash = chained.pop("record_hash", None)
        canonical = json.dumps(chained, sort_keys=True, separators=(",", ":"))
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if recorded_hash != expected_hash:
            raise HarborMeteringError("proxy ledger record hash is invalid")
        previous_hash = expected_hash
    if (
        seal.get("record_count") != len(requests)
        or seal.get("last_sequence") != len(requests)
        or seal.get("root_hash") != previous_hash
    ):
        raise HarborMeteringError("proxy ledger seal does not match its records")
    return requests, seal


def _proxy_totals(
    records: list[dict[str, Any]],
) -> tuple[UsageCounters, list[str]]:
    errors: list[str] = []
    totals = {
        "input_uncached": 0,
        "cache_read": 0,
        "cache_write": 0,
        "output": 0,
    }
    for index, record in enumerate(records, 1):
        if record.get("capture_truncated"):
            errors.append(f"request_{index}_capture_truncated")
        if record.get("error"):
            errors.append(f"request_{index}_proxy_error")
        usage = record.get("usage")
        split = proxy_split_from_usage(usage)
        values = {
            "input_uncached": split.get("tokens_proxy_input_uncached"),
            "cache_read": split.get("tokens_proxy_cache_read"),
            "cache_write": split.get("tokens_proxy_cache_write"),
            "output": split.get("tokens_proxy_output"),
        }
        if not isinstance(usage, dict) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values.values()
        ):
            errors.append(f"request_{index}_usage_incomplete")
            continue
        for name, value in values.items():
            totals[name] += value
    if errors:
        return UsageCounters(len(records), None, None, None), errors
    return (
        UsageCounters(
            calls=len(records),
            input_tokens=(
                totals["input_uncached"]
                + totals["cache_read"]
                + totals["cache_write"]
            ),
            cache_tokens=totals["cache_read"],
            output_tokens=totals["output"],
        ),
        errors,
    )


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _member(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise HarborMeteringError(f"metering path is not a real directory: {path}")
    os.chmod(path, 0o700)


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    pending = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(pending, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            pending.unlink()
        except FileNotFoundError:
            pass
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
