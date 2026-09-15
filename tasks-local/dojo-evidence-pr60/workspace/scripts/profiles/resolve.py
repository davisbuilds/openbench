
""" """



























from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from .definitions import (
    BASELINE,
    INSPECTION,
    SUPPORTED_HARNESSES,
    Equivalence,
    Profile,
    resolved_members,
)


class SelectionError(ValueError):
    """ """







    def __init__(self, code: SelectionErrorCode, message: str) -> None:
        super().__init__(f"[{code.value}] {message}")
        self.code = code


class SelectionErrorCode(str, Enum):
    UNKNOWN_PROFILE = "unknown-profile"
    MISSING_CORE = "missing-core"
    NO_OVERLAY = "no-overlay"
    REPEATED_TOKEN = "repeated-token"
    FULL_NOT_EXCLUSIVE = "full-not-exclusive"


@dataclass(frozen=True)
class Resolution:
    """ """

    selection: tuple[str, ...]
    members: tuple[str, ...]
    identity: str


@dataclass(frozen=True)
class Suppression:
    """ """

    skill: str
    bundled_entry: str
    evidence: str


@dataclass(frozen=True)
class Collision:
    """ """





    skill: str
    bundled_entry: str


@dataclass(frozen=True)
class HarnessResolution:
    """ """

    harness: str
    realized: tuple[str, ...]
    suppressed: tuple[Suppression, ...]
    collisions: tuple[Collision, ...]


def resolve(selection: list[str] | tuple[str, ...], definitions: dict[str, Profile], catalog: dict[str, dict]) -> Resolution:
    """ """





    tokens = tuple(selection)

    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            raise SelectionError(
                SelectionErrorCode.REPEATED_TOKEN,
                f"profile {token!r} named more than once in one selection",
            )
        seen.add(token)

    unknown = sorted(t for t in tokens if t not in definitions)
    if unknown:
        raise SelectionError(
            SelectionErrorCode.UNKNOWN_PROFILE,
            f"unknown profile(s) {', '.join(unknown)}; known: {', '.join(sorted(definitions))}",
        )

    if INSPECTION in seen and len(seen) > 1:
        others = sorted(seen - {INSPECTION})
        raise SelectionError(
            SelectionErrorCode.FULL_NOT_EXCLUSIVE,
            f"{INSPECTION!r} is a fixed inspection profile and cannot combine with {', '.join(others)}",
        )

    if INSPECTION not in seen:
        if BASELINE not in seen:
            raise SelectionError(
                SelectionErrorCode.MISSING_CORE,
                f"every deployable composition includes {BASELINE!r}; selection was {', '.join(sorted(seen)) or 'empty'}",
            )
        if seen == {BASELINE}:
            raise SelectionError(
                SelectionErrorCode.NO_OVERLAY,
                f"a {BASELINE!r}-only selection is not a deployable composition; name at least one capability overlay",
            )

    members: set[str] = set()
    for token in sorted(seen):
        members.update(resolved_members(definitions[token], catalog))

    normalized = tuple(sorted(seen))
    return Resolution(
        selection=normalized,
        members=tuple(sorted(members)),
        identity=profile_identity(normalized, definitions),
    )


def profile_identity(selection: tuple[str, ...] | list[str], definitions: dict[str, Profile]) -> str:
    """ """











    normalized = sorted(set(selection))
    bodies = [
        {
            "name": definitions[name].name,
            "kind": definitions[name].kind,
            "members": list(definitions[name].members),
        }
        for name in normalized
    ]
    blob = json.dumps(
        {"selection": normalized, "definitions": bodies},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_for_harness(
    resolution: Resolution,
    equivalences: dict[str, Equivalence],
    harness: str,
    bundled_entries: tuple[str, ...] | list[str] = (),
) -> HarnessResolution:
    """ """







    if harness not in SUPPORTED_HARNESSES:
        raise SelectionError(
            SelectionErrorCode.UNKNOWN_PROFILE,
            f"unknown harness {harness!r}; supported: {', '.join(SUPPORTED_HARNESSES)}",
        )

    realized: list[str] = []
    suppressed: list[Suppression] = []
    collisions: list[Collision] = []
    listed = set(bundled_entries)

    for member in resolution.members:
        declared = equivalences.get(member)
        if declared is not None:
            suppressed.append(
                Suppression(
                    skill=member,
                    bundled_entry=declared.bundled_entry,
                    evidence=declared.evidence,
                )
            )
            continue
        if member in listed:
            
            
            
            collisions.append(Collision(skill=member, bundled_entry=member))
        realized.append(member)

    return HarnessResolution(
        harness=harness,
        realized=tuple(realized),
        suppressed=tuple(suppressed),
        collisions=tuple(collisions),
    )


def realization_identity(
    profile_id: str,
    canonical_revision: str,
    target_identity: str,
    harness_model_version: str,
    budget_policy_identity: str,
    equivalence_identity: str,
) -> str:
    """ """







    blob = json.dumps(
        {
            "profile_identity": profile_id,
            "canonical_revision": canonical_revision,
            "target_identity": target_identity,
            "harness_model_version": harness_model_version,
            "budget_policy_identity": budget_policy_identity,
            "equivalence_identity": equivalence_identity,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
