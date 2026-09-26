
""" """




























from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

from .probe_codex import (
    MAX_DEFAULT_CONTEXT_SKILL_DESCRIPTION_CHARS,
    TRUNCATED_SKILL_DESCRIPTION_SUFFIX,
    alias_table_cost_tokens,
    line_cost_tokens,
)




CEILING_BASIS_POINTS = 9_000
BASIS = 10_000

CLAUDE_ELLIPSIS = "…"


class Degradation(str, Enum):
    """ """

    CODEX_CLIPPED = "codex-clipped-no-marker"
    CODEX_PRECAP = "codex-precap-ellipsis"
    CODEX_OMITTED = "codex-skill-omitted"
    CLAUDE_ELLIPSIS_TRUNCATED = "claude-ellipsis-truncated"
    CLAUDE_DESCRIPTION_REMOVED = "claude-description-removed"


class Verdict(str, Enum):
    DEPLOYABLE = "deployable"
    NONCONFORMANT = "nonconformant"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Policy:
    """ """

    harness: str
    harness_version: str
    model: str
    unit: str
    limit: int
    context_window: int | None
    window_field: str | None
    estimator: str
    provenance: str
    measured: str
    probe: str
    deployable: bool
    shadows_by_name: bool
    project_scope_root: str





    limit_basis: str = "vendor"
    declared_surfaces: tuple[str, ...] = ()
    identity: str = ""

    @property
    def provisional(self) -> bool:
        return self.limit_basis not in ("observed", "vendor-corroborated")

    def accepts_surface(self, surface: str | None) -> bool:
        """ """




        if not self.declared_surfaces:
            return True
        return surface in self.declared_surfaces

    def with_identity(self) -> "Policy":
        payload = {
            k: v for k, v in self.__dict__.items() if k != "identity"
        }
        blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return Policy(**{**payload, "identity": hashlib.sha256(blob.encode()).hexdigest()})


@dataclass
class Assessment:
    """ """

    policy: Policy
    demand: int
    limit: int
    unit: str
    verdict: Verdict
    degradations: tuple[Degradation, ...] = ()
    reason: str = ""
    entries_scored: int = 0



    surface: str | None = None

    @property
    def basis_points(self) -> int:
        """ """
        return (self.demand * BASIS) // self.limit if self.limit else 0

    @property
    def gating(self) -> bool:
        """ """

















        return (
            self.policy.deployable
            and not self.policy.provisional
        )


def load_policy(path: Path | str) -> Policy:
    """ """
    data = yaml.safe_load(Path(path).read_text())
    required = (
        "harness", "harness_version", "model", "unit", "limit", "estimator",
        "provenance", "measured", "probe", "deployable", "shadows_by_name",
        "project_scope_root",
    )
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"{Path(path).name}: policy is missing {', '.join(missing)}")
    if data["unit"] not in ("tokens", "characters"):
        raise ValueError(f"{Path(path).name}: unit must be 'tokens' or 'characters', got {data['unit']!r}")
    return Policy(
        harness=data["harness"],
        harness_version=data["harness_version"],
        model=data["model"],
        unit=data["unit"],
        limit=int(data["limit"]),
        context_window=data.get("context_window"),
        window_field=data.get("window_field"),
        estimator=data["estimator"],
        provenance=data["provenance"],
        measured=str(data["measured"]),
        probe=data["probe"],
        deployable=bool(data["deployable"]),
        shadows_by_name=bool(data["shadows_by_name"]),
        project_scope_root=data["project_scope_root"],
        limit_basis=str(data.get("limit_basis", "vendor")).split()[0],
        declared_surfaces=tuple(data.get("declared_surfaces") or ()),
    ).with_identity()


def entry_cost(name: str, description: str, policy: Policy, locator: str = "") -> int:
    """ """





    if policy.unit == "tokens":
        return line_cost_tokens(f"- {name}: {description} (file: {locator})")
    return len(f"- {name}: {description}") + 1


def demand(entries: list[dict], policy: Policy, root_lines: list[str] | None = None) -> int:
    """ """




    total = sum(
        entry_cost(e["name"], e.get("source_description") or "", policy, e.get("locator", ""))
        for e in entries
    )
    if policy.unit == "tokens" and root_lines:
        total += alias_table_cost_tokens(root_lines)
    return total


def detect_degradation(entries: list[dict], policy: Policy, warning: str | None = None,
                       candidate_count: int | None = None) -> tuple[Degradation, ...]:
    """ """






    found: set[Degradation] = set()

    for entry in entries:









        if "listed_description" not in entry:
            continue

        listed = entry["listed_description"]
        source = entry.get("source_description")
        exempt = entry.get("exempt", False)

        if policy.unit == "tokens":
            if listed and source and listed != source:
                if source.startswith(listed.removesuffix(TRUNCATED_SKILL_DESCRIPTION_SUFFIX)) and \
                        listed.endswith(TRUNCATED_SKILL_DESCRIPTION_SUFFIX) and \
                        len(source) > MAX_DEFAULT_CONTEXT_SKILL_DESCRIPTION_CHARS:
                    found.add(Degradation.CODEX_PRECAP)
                elif source.startswith(listed):
                    found.add(Degradation.CODEX_CLIPPED)
        else:
            if listed is None and source and not exempt:
                found.add(Degradation.CLAUDE_DESCRIPTION_REMOVED)
            elif listed and listed.rstrip().endswith(CLAUDE_ELLIPSIS):
                found.add(Degradation.CLAUDE_ELLIPSIS_TRUNCATED)




    if warning and "Exceeded skills context budget" in warning:
        found.add(Degradation.CODEX_OMITTED)
    if candidate_count is not None and len(entries) < candidate_count:
        found.add(Degradation.CODEX_OMITTED)

    return tuple(sorted(found, key=lambda d: d.value))


def assess(entries: list[dict], policy: Policy, *, root_lines: list[str] | None = None,
           warning: str | None = None, candidate_count: int | None = None,
           surface: str | None = None) -> Assessment:
    """ """









    if not entries:
        return Assessment(
            policy=policy, demand=0, limit=policy.limit, unit=policy.unit,
            verdict=Verdict.UNSUPPORTED, reason="no entries observed", surface=surface,
        )
    if not policy.limit:
        return Assessment(
            policy=policy, demand=0, limit=0, unit=policy.unit,
            verdict=Verdict.UNSUPPORTED, reason="no authoritative limit", surface=surface,
        )

    cost = demand(entries, policy, root_lines)
    shapes = detect_degradation(entries, policy, warning, candidate_count)

    if shapes:
        verdict, reason = Verdict.NONCONFORMANT, f"degraded: {', '.join(s.value for s in shapes)}"
    elif cost * BASIS <= policy.limit * CEILING_BASIS_POINTS:
        verdict, reason = Verdict.DEPLOYABLE, ""
    else:
        verdict, reason = Verdict.NONCONFORMANT, f"over ceiling: {(cost * BASIS) // policy.limit} bps"

    return Assessment(
        policy=policy, demand=cost, limit=policy.limit, unit=policy.unit,
        verdict=verdict, degradations=shapes, reason=reason, entries_scored=len(entries),
        surface=surface,
    )
