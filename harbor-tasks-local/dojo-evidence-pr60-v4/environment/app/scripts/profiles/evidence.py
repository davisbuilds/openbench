
""" """
























from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from .budget import Assessment, Policy, Verdict
from .observe import Observation
from .resolve import HarnessResolution, Resolution



SC06_FIELDS = (
    "assertions",
    "budget",
    "canonical_revision",
    "collisions",
    "drift",
    "foreign_entries",
    "harness",
    "legacy_topologies",
    "membership",
    "plugin_entries",
    "profile",
    "realization_identity",
    "resolved_members",
    "routing_coverage",
    "shadowed_names",
    "state",
    "suppressed",
)


class ExitCode(IntEnum):
    """ """






    CONFORMANT = 0
    INCOMPLETE = 1
    NONCONFORMANT = 2





STATE_CONFORMANT = "conformant"
STATE_NONCONFORMANT = "nonconformant"
STATE_UNPROFILED = "unprofiled"
STATE_UNSUPPORTED = "unsupported"


@dataclass
class Evidence:
    """ """

    payload: dict
    partial: bool = False
    envelope: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @property
    def exit_code(self) -> ExitCode:
        if self.partial:
            return ExitCode.INCOMPLETE
        if self.payload.get("state") == STATE_CONFORMANT:
            return ExitCode.CONFORMANT
        return ExitCode.NONCONFORMANT

    def __post_init__(self) -> None:


        if self.partial and self.payload.get("state") == STATE_CONFORMANT:
            raise ValueError("a partial report cannot be conformant")


def detect_legacy_topologies(observation: Observation, canonical_root: Path | None = None) -> list[dict]:
    """ """










    found: list[dict] = []
    managed = [e for e in observation.entries if e.origin == "dojo-managed"]

    for root, target in sorted(observation.symlinked_scope_roots):
        if canonical_root is None or Path(target) == Path(canonical_root).resolve():
            found.append({
                "kind": "whole-catalog-link",
                "root": root,
                "target": target,
                "detail": "scope root is a symlink into the canonical catalog, exposing all of it",
            })

    concrete = sorted(e.name for e in managed if e.is_symlink is False)
    if concrete:
        found.append({"kind": "concrete-secondary-copy", "entries": concrete})

    drifted = sorted(
        e.name for e in managed
        if e.source_description is not None
        and e.listed_description is not None
        and e.listed_description != e.source_description
    )
    if drifted:
        found.append({"kind": "version-skewed-content", "entries": drifted})

    return found


def dirty_state(repo_root: Path, selected_members: tuple[str, ...],
                selected_definitions: tuple[str, ...] = ()) -> list[str]:
    """ """






    try:
        out = subprocess.run(




            ["git", "-C", str(repo_root), "status", "--porcelain", "-uall"],
            capture_output=True, text=True, check=False, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ["<git unavailable: source revision unverifiable>"]





    selected_paths = {f"skills/{name}/" for name in selected_members}
    selected_paths |= {f"profiles/{name}.yaml" for name in selected_definitions}
    dirty = []
    for line in out.splitlines():
        path = line[3:].strip()
        if any(path.startswith(prefix) for prefix in selected_paths):
            dirty.append(path)
    return sorted(dirty)


def canonical_revision(repo_root: Path) -> str | None:
    """ """





    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def build_evidence(
    resolution: Resolution | None,
    harness_resolution: HarnessResolution | None,
    observation: Observation,
    assessment: Assessment,
    policy: Policy,
    *,
    repo_root: Path,
    canonical_root: Path | None = None,
    realization_id: str | None = None,
    equivalence_id: str | None = None,
    routing_coverage: dict | None = None,
    partial: bool = False,
) -> Evidence:
    """ """




    revision = canonical_revision(repo_root)
    members = resolution.members if resolution else ()
    expected = set(harness_resolution.realized) if harness_resolution else set(members)
    dirty = dirty_state(
        repo_root, members,
        selected_definitions=resolution.selection if resolution else (),
    )






    observed_managed = {e.name for e in observation.entries if e.origin == "dojo-managed"}
    missing = sorted(expected - observed_managed) if resolution else []
    unexpected = sorted(observed_managed - expected) if resolution else []

    if partial:
        state = STATE_UNSUPPORTED
    elif revision is None:
        state = STATE_UNSUPPORTED
    elif resolution is None:
        state = STATE_UNPROFILED
    elif dirty:
        state = STATE_UNSUPPORTED
    elif observation.unsupported:



        state = STATE_UNSUPPORTED
    elif missing or unexpected:
        state = STATE_NONCONFORMANT
    elif assessment.verdict is Verdict.DEPLOYABLE:
        state = STATE_CONFORMANT
    elif assessment.verdict is Verdict.UNSUPPORTED:
        state = STATE_UNSUPPORTED
    else:
        state = STATE_NONCONFORMANT

    payload = {
        "state": state,
        "canonical_revision": revision,
        "profile": {
            "selection": list(resolution.selection) if resolution else [],
            "identity": resolution.identity if resolution else None,
            "dirty_selected_paths": dirty,
        },
        "realization_identity": realization_id,
        "resolved_members": sorted(members),



        "suppressed": sorted(
            (
                {"skill": s.skill, "bundled_entry": s.bundled_entry, "evidence": s.evidence}
                for s in (harness_resolution.suppressed if harness_resolution else ())
            ),
            key=lambda d: d["skill"],
        ),
        "collisions": sorted(
            (
                {"skill": c.skill, "bundled_entry": c.bundled_entry}
                for c in (harness_resolution.collisions if harness_resolution else ())
            ),
            key=lambda d: d["skill"],
        ),
        "harness": {
            "name": policy.harness,
            "version": policy.harness_version,
            "model": policy.model,
            "policy_identity": policy.identity,
            "deployable_pair": policy.deployable,
            "shadows_by_name": policy.shadows_by_name,
        },
        "budget": {
            "unit": assessment.unit,
            "limit": assessment.limit,
            "demand": assessment.demand,
            "basis_points": assessment.basis_points,
            "headroom": assessment.limit - assessment.demand,
            "verdict": assessment.verdict.value,
            "gating": assessment.gating,
            "cost_basis": observation.cost_basis_counts,
            "reason": assessment.reason,
        },
        "drift": {
            "degradations": [d.value for d in assessment.degradations],
            "unsupported": sorted(observation.unsupported),
        },
        "foreign_entries": sorted(e.name for e in observation.entries if e.origin == "foreign"),
        "plugin_entries": sorted(e.name for e in observation.entries if e.origin == "plugin"),
        "shadowed_names": list(observation.duplicated_names),
        "membership": {
            "expected": sorted(expected) if resolution else [],
            "missing": missing,
            "unexpected": unexpected,
        },
        "legacy_topologies": detect_legacy_topologies(observation, canonical_root),
        "routing_coverage": routing_coverage or {"skills_with_fixtures": [], "reported": False},
        "assertions": {"executed": 0, "outcomes": []},
        "equivalence_identity": equivalence_id,
    }


    payload["profile"]["equivalence_identity"] = payload.pop("equivalence_identity")

    return Evidence(payload=payload, partial=partial)


def build_evidence_json(*args, **kwargs) -> str:
    return build_evidence(*args, **kwargs).to_json()
