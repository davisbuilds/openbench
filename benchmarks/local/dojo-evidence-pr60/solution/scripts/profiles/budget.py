#!/usr/bin/env python3
"""Compute listing cost in each harness's own arithmetic, and detect degradation.

Three rules, each of which this program learned by getting it wrong:

1. **Cost comes from untruncated source, never from a rendered listing.** A
   harness that elides to fit produces output that always fits. Measured live:
   Claude Code rendered 8,046 characters against an 8,000 budget while true
   demand was 23,287 — a verifier reading the rendering would report 101% for a
   291% listing and certify the exact failure it exists to catch.

2. **Two deployable harnesses means two policies with different units**, not one
   with a scaling factor. Codex budgets in tokens, Claude Code in characters.
   Converting on the Claude path would introduce an error the harness itself
   never makes.

3. **Degradation is nonconformance regardless of cost.** Five shapes across the
   two harnesses; the two most severe are invisible — Codex clips mid-word with
   no marker, and Claude Code drops descriptions entirely so there is nothing to
   compare a prefix against.

The Codex primitives are imported from `probe_codex` rather than reimplemented.
Two of them are not what a reasonable reading of `render.rs` suggests: only skill
lines are charged, and the alias table cost is a rounded difference of two whole
rendered bodies. A second implementation is a second thing that can disagree.

Contract: docs/specs/2026-07-27-distribution-profiles-spec.md (SC-03, SC-04,
EV-NEG-02, EV-LEG-03).
"""

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

# SC-04's deployability ceiling, as exact integer basis points. Compared as
# `demand * 10_000 <= limit * 9_000` so no float rounding can move a verdict
# across the boundary.
CEILING_BASIS_POINTS = 9_000
BASIS = 10_000

CLAUDE_ELLIPSIS = "…"


class Degradation(str, Enum):
    """The five observable shapes. Named so evidence can say which occurred."""

    CODEX_CLIPPED = "codex-clipped-no-marker"        # (a) budget-driven, invisible
    CODEX_PRECAP = "codex-precap-ellipsis"           # (b) 1,024-char cap, marked "..."
    CODEX_OMITTED = "codex-skill-omitted"            # (c) third tier, warns in-prompt
    CLAUDE_ELLIPSIS_TRUNCATED = "claude-ellipsis-truncated"   # (d) past maxDescChars
    CLAUDE_DESCRIPTION_REMOVED = "claude-description-removed"  # (e) bare name


class Verdict(str, Enum):
    DEPLOYABLE = "deployable"
    NONCONFORMANT = "nonconformant"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Policy:
    """A harness/model budget policy, loaded from reviewed YAML."""

    harness: str
    harness_version: str
    model: str
    unit: str                    # "tokens" | "characters"
    limit: int
    context_window: int | None
    window_field: str | None
    estimator: str
    provenance: str
    measured: str
    probe: str
    deployable: bool             # declared in use, per spec revision 11
    shadows_by_name: bool
    project_scope_root: str
    # SC-04, revision 13. "observed" = established by saturation; two renders of
    # different inputs that both saturate must total the same number.
    # "vendor-corroborated" = a vendor constant checked against behaviour.
    # Bare "vendor" is PROVISIONAL and may not make a pair deployable — that is
    # the mistake that made a 100%-of-budget Codex target read as 74%.
    limit_basis: str = "vendor"
    declared_surfaces: tuple[str, ...] = ()
    identity: str = ""

    @property
    def provisional(self) -> bool:
        return self.limit_basis not in ("observed", "vendor-corroborated")

    def accepts_surface(self, surface: str | None) -> bool:
        """Only a surface declared in use can make a pair deployable.

        An empty declaration accepts anything, for the harnesses that have not
        been shown to render differently per entry point. Codex has.
        """
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
    """The result of scoring one target against one policy."""

    policy: Policy
    demand: int
    limit: int
    unit: str
    verdict: Verdict
    degradations: tuple[Degradation, ...] = ()
    reason: str = ""
    entries_scored: int = 0
    # The invocation surface this observation came from. `None` means the caller
    # did not say — which a policy declaring surfaces must treat as undeclared,
    # not as acceptable.
    surface: str | None = None

    @property
    def basis_points(self) -> int:
        """Utilization in basis points, exact integer arithmetic."""
        return (self.demand * BASIS) // self.limit if self.limit else 0

    @property
    def gating(self) -> bool:
        """Whether this verdict may fail a build.

        A policy that is *declared but not deployable* — Claude Code at 200k, per
        spec revision 11 — is scored and reported, never gating. A session that
        lands there must be told; a suite must not fail for a path nobody runs.

        A **provisional** limit also cannot gate (spec revision 13): a limit read
        from a vendor catalog and never checked against behaviour overstated
        Codex's by 36%, and gating on it would fail builds against a ceiling that
        does not exist.

        And a verdict from an **undeclared surface** cannot gate either. Building
        `Policy.accepts_surface` without consulting it here was the whole defect
        in miniature: the capability existed, a unit test covered it, and the
        assessment path never called it — so an `exec`-derived result under a
        TUI-only policy still gated, preserving exactly the wrong-surface failure
        this work exists to remove.
        """
        return (
            self.policy.deployable
            and not self.policy.provisional
            and self.policy.accepts_surface(self.surface)
        )


def load_policy(path: Path | str) -> Policy:
    """Load a reviewed policy file and compute its identity."""
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
    """Cost of one rendered entry, in the policy's own unit.

    Codex renders `- name: description (file: path)` and charges tokens over
    UTF-8 bytes. Claude Code renders `- name: description` and charges
    characters. Neither is converted into the other's unit at any point.
    """
    if policy.unit == "tokens":
        return line_cost_tokens(f"- {name}: {description} (file: {locator})")
    return len(f"- {name}: {description}") + 1


def demand(entries: list[dict], policy: Policy, root_lines: list[str] | None = None) -> int:
    """Total cost of the listing the harness *would* render, unelided.

    `entries` carry `source_description` — the untruncated frontmatter — never a
    description read back from a rendered listing.
    """
    total = sum(
        entry_cost(e["name"], e.get("source_description") or "", policy, e.get("locator", ""))
        for e in entries
    )
    if policy.unit == "tokens" and root_lines:
        total += alias_table_cost_tokens(root_lines)
    return total


def detect_degradation(entries: list[dict], policy: Policy, warning: str | None = None,
                       candidate_count: int | None = None) -> tuple[Degradation, ...]:
    """Every observable degradation shape, per harness.

    Shape (e) — a Claude Code entry rendered as a bare name — cannot be caught by
    comparing a listed description against its source, because there is no listed
    description. It is detected by *absence*, which is why a prefix-comparison
    detector would silently miss the most severe case.
    """
    found: set[Degradation] = set()

    for entry in entries:
        # **Not observed is not observed-absent.** An entry carries
        # `listed_description` only if a probe actually rendered it; the key
        # being missing means nobody looked, while the key present and `None`
        # means the harness rendered a bare name. Conflating them made a
        # *hypothetical* fit proof — `core` plus an overlay, scored before any
        # target exists — report every member as description-removed, which is
        # the detector inventing a degradation out of the absence of a
        # measurement. Caught by the SC-03 fit proof failing on entries that had
        # never been rendered at all.
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

    # (c) Codex omission, detectable two ways. Both are used: the in-prompt
    # warning is authoritative when present, and a listed count below the
    # candidate count catches it when the warning text changes.
    if warning and "Exceeded skills context budget" in warning:
        found.add(Degradation.CODEX_OMITTED)
    if candidate_count is not None and len(entries) < candidate_count:
        found.add(Degradation.CODEX_OMITTED)

    return tuple(sorted(found, key=lambda d: d.value))


def assess(entries: list[dict], policy: Policy, *, root_lines: list[str] | None = None,
           warning: str | None = None, candidate_count: int | None = None,
           surface: str | None = None) -> Assessment:
    """Score a target: cost from source, degradation from observation.

    Degradation makes a target nonconformant **regardless of computed cost** —
    a listing that fits after the harness elided it is not a listing that fits.

    `surface` names the invocation surface the entries were observed on. A policy
    declaring surfaces refuses anything else outright rather than scoring it,
    because a number from the wrong code path is not a weaker measurement — it is
    a measurement of something else.
    """
    if not policy.accepts_surface(surface):
        return Assessment(
            policy=policy, demand=0, limit=policy.limit, unit=policy.unit,
            verdict=Verdict.UNSUPPORTED, surface=surface,
            reason=(f"surface {surface!r} is not declared for {policy.harness}; "
                    f"declared: {', '.join(policy.declared_surfaces)}"),
        )
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
