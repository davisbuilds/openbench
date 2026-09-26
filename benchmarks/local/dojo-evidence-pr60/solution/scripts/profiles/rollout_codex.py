#!/usr/bin/env python3
"""Observe Codex from the record of what it *sent*, not from a re-render.

Task 0 built a probe on ``codex debug prompt-input`` and every later task trusted
it. On 2026-08-04 a live TUI session printed *"Skill descriptions were
shortened"* while that probe reported 76% of budget and zero degradation for the
same directory, model, and minute. Both were internally correct. ``debug
prompt-input`` renders the ``codex exec`` path, which does **not** load
account-synced connector plugins; the interactive TUI does. The gap was 69 of 110
entries.

The probe's parsing was right, its port of Codex's arithmetic was right, and it
agreed with Codex's own charged figure to 0.15%. **It was right about a session
nobody opens.** Precision is not fidelity.

Codex writes each session's rendered prompt to
``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``, including the whole
``<skills_instructions>`` block and the session's ``originator``. That is a
record of what was sent rather than a re-render of what might be, and it makes
the surface split observable instead of invisible. This module reads it and
reuses ``probe_codex.parse_block`` unchanged — a second parser would be a second
thing that can disagree.

Two further corrections live here, both found by measuring rather than reasoning:

**The limit is disclosed by saturation, and it is a property of the build.** Codex
spends the listing budget to the last token, so two renders of different inputs
that both saturate must total the same number — and that number is the limit.
Sweeping 89 parseable rollouts across twelve CLI builds:

===========  ===============================================
build        saturated total (= ceiling)
===========  ===============================================
0.139.0      5,358
0.142.3/4    5,534
0.143.0      5,440   = 2% x 272,000, the vendor formula
0.144.1      5,440 **and** 7,440 (a 372,000-window model)
0.144.6      5,440
0.145.0      ~4,000
0.146.0      4,000
===========  ===============================================

So ``2% x context_window`` was **correct through 0.144.x**, including the
372,000-window case — and **0.145.0 changed it**. An earlier revision of this
module claimed the budget "does not move with the model at all"; that was an
overclaim from two models on one build, corrected once the history was swept.
What survives is stronger and is what ``derive_limit`` enforces: a ceiling
belongs to one build, samples from two builds may never be pooled, and a limit
read from the catalog without a saturation check is provisional.

**Demand outside local control must be attributed separately, and compared as a
set.** Account connectors appear, change, and vanish with no local action:
removing two freed 734 tokens on 2026-08-06 while a Codex desktop update added
723 back the same day. Net 11 tokens, entry count unchanged at 56 — so a check
comparing *totals* would have reported that nothing happened.

Contract: docs/specs/2026-07-27-distribution-profiles-spec.md (SC-04).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .probe_codex import Listing, _absolute, parse_block

BLOCK_OPEN = "<skills_instructions>"
BLOCK_CLOSE = "</skills_instructions>"

SURFACE_TUI = "codex-tui"
SURFACE_EXEC = "codex_exec"
SURFACE_PROBE = "exec"  # what probe_codex observes; an alias of the exec path

_BLOCK_RE = re.compile(rf"{BLOCK_OPEN}.*?{BLOCK_CLOSE}", re.S)

# Origin is read from the locator, because a fixture's paths do not exist on the
# machine reading it. Where the path *is* present, `.codex-remote-plugin-install.json`
# confirms an account-synced entry from the plugin's own payload rather than from
# a path heuristic; the marker is checked when available and never required.
CONNECTOR_SEGMENT = "/plugins/cache/openai-curated-remote/"
PLUGIN_CACHE_SEGMENT = "/plugins/cache/"
BUNDLED_SEGMENTS = ("/.codex/skills/.system/", "codex-primary-runtime")
DOJO_SEGMENT = "/.agents/skills/"
REMOTE_MARKER = ".codex-remote-plugin-install.json"

ORIGIN_DOJO = "dojo-managed"
ORIGIN_BUNDLED = "harness-bundled"
ORIGIN_CONNECTOR = "connector"
ORIGIN_PLUGIN = "plugin"
ORIGIN_FOREIGN = "foreign"

# Origins the operator cannot govern from this repository. `connector` is the
# sharp case: it is governed by a ChatGPT account setting, is invisible to
# `codex plugin list` (which reports such entries `not installed` while they are
# listing), and its local `enabled = false` config key is **inert** — verified by
# controlled test on 2026-08-04, google-drive disabled with its cache intact and
# openai-developers left untouched as a control.
UNCONTROLLED_ORIGINS = frozenset({ORIGIN_CONNECTOR, ORIGIN_PLUGIN, ORIGIN_BUNDLED})


@dataclass(frozen=True)
class RolloutMeta:
    """Everything the rollout states about the session that produced a listing."""

    path: Path
    surface: str
    cli_version: str
    cwd: str
    model: str | None
    session_id: str | None = None

    @property
    def harness_build(self) -> str:
        """The identity a verdict is bounded by (SC-04, revision 14).

        A desktop update introduced a whole plugin marketplace worth 18% of
        budget with no local action, so a verdict measured against one build says
        nothing about the next.
        """
        return f"{self.cli_version}/{self.model or 'unknown-model'}"


@dataclass
class RolloutObservation:
    """One session's listing, with the surface that produced it."""

    meta: RolloutMeta
    listing: Listing

    @property
    def charged_tokens(self) -> int:
        return self.listing.charged_tokens

    @property
    def entry_names(self) -> frozenset[str]:
        return frozenset(e.name for e in self.listing.entries)

    def absolute_locator(self, locator: str) -> str:
        """Expand an `rN/...` alias against the roots table.

        **Classification must never run on a raw locator.** In alias render mode
        every locator is `rN/<rest>`, which matches none of the absolute path
        segments, so every entry classifies as `foreign` and the uncontrolled set
        comes back empty — a confident zero. Caught here by a test asserting the
        set was non-empty against a fixture known to contain connectors.
        """
        return _absolute(locator, self.listing.root_lines)

    def origin_of(self, locator: str) -> str:
        return classify_locator(self.absolute_locator(locator))

    @property
    def uncontrolled_entries(self) -> frozenset[str]:
        """Entries the operator does not govern, as `origin:name` identities.

        Returned as a **set**, never a count or a sum. On 2026-08-06 two
        uncontrolled changes cancelled to 11 tokens with the entry count
        unchanged at 56; anything comparing totals would have reported that
        nothing happened.

        **Qualified by origin, because a bare name is ambiguous.** Codex does not
        shadow across roots, so one name can be listed twice from two origins and
        charged twice: `skill-creator` is bundled by Codex *and* shipped by dojo,
        which is exactly the collision the harness-equivalence declaration exists
        to record. Keyed by name alone, that one entry made the whole catalog
        look partly ungovernable. The version segment is deliberately excluded —
        a plugin version bump is a harness-build change, caught by
        `harness_build`, not a change in which entries are present.
        """
        return frozenset(
            f"{origin}:{e.name}"
            for e in self.listing.entries
            for origin in (self.origin_of(e.locator),)
            if origin in UNCONTROLLED_ORIGINS
        )


def classify_locator(locator: str) -> str:
    """Origin from the locator, with the remote marker as confirmation when present."""
    if CONNECTOR_SEGMENT in locator:
        return ORIGIN_CONNECTOR
    if DOJO_SEGMENT in locator:
        return ORIGIN_DOJO
    if any(seg in locator for seg in BUNDLED_SEGMENTS):
        return ORIGIN_BUNDLED
    if PLUGIN_CACHE_SEGMENT in locator:
        # A plugin cache entry whose payload carries a remote-install marker is
        # an account-synced connector regardless of which cache it landed in.
        probe = Path(locator)
        for parent in list(probe.parents)[:6]:
            if (parent / REMOTE_MARKER).exists():
                return ORIGIN_CONNECTOR
        return ORIGIN_PLUGIN
    return ORIGIN_FOREIGN


HARNESS_CONTEXT_ROLE = "developer"


def _is_harness_context(record: dict) -> bool:
    """Whether this record is the harness's own rendered context.

    Keyed on the message role rather than on the record type, because the type
    (`response_item`) is shared with ordinary conversation turns.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    return payload.get("role") == HARNESS_CONTEXT_ROLE


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_strings(value)


def _meta_from(path: Path, lines: list[str]) -> RolloutMeta:
    fields: dict[str, str] = {}
    for raw in lines[:60]:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for key in ("originator", "cli_version", "cwd", "model", "id"):
            if key in fields:
                continue
            match = re.search(rf'"{key}":\s*"([^"]*)"', raw)
            if match:
                fields[key] = match.group(1)
        del record
    return RolloutMeta(
        path=path,
        surface=fields.get("originator", "unknown"),
        cli_version=fields.get("cli_version", "unknown"),
        cwd=fields.get("cwd", ""),
        model=fields.get("model"),
        session_id=fields.get("id"),
    )


def read_rollout(path: Path | str) -> RolloutObservation | None:
    """Parse one rollout, or None when it carries no skills block.

    A session that never made a model call has sent nothing and recorded
    nothing — that is an absent observation, not an empty one, and the caller
    must be able to tell the difference.
    """
    path = Path(path)
    lines = path.read_text(errors="replace").splitlines()
    block = None
    for raw in lines:
        if BLOCK_OPEN not in raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # **Only the harness-authored context record may supply the listing.**
        # An earlier version walked every string in every record and took the
        # first hit. A conversation that merely *discusses* a skills listing then
        # supplies the measurement: across this machine's 309 rollouts the block
        # appears 238 times as `developer` (the real context) but also twice as
        # `user`, twice as `assistant`, 136 times inside `compacted` summaries,
        # and 67 times in untyped response items. One live session —
        # 2026-07-28T17-23-34 — has a **user-pasted block first**, so the naive
        # reader would have measured pasted text as the effective catalog. The
        # historical table in the spec used the sibling session recorded 14
        # seconds later and was correct by luck.
        #
        # `compacted` is excluded deliberately: it is harness-authored but is a
        # summary of an earlier turn, so its listing may be stale.
        if not _is_harness_context(record):
            continue
        for text in _walk_strings(record):
            match = _BLOCK_RE.search(text)
            if match:
                block = match.group(0)
                break
        if block:
            break
    if block is None:
        return None
    return RolloutObservation(meta=_meta_from(path, lines), listing=parse_block(block))


def default_sessions_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def find_rollouts(sessions_root: Path | str | None = None) -> list[Path]:
    """Every rollout, newest first by modification time.

    Deliberately not a `find -newermt` style filter: on BSD `find` that flag
    silently matches nothing rather than erroring, and it produced two false
    "no rollout exists" reports during this task. Sort here, filter in Python.
    """
    root = Path(sessions_root) if sessions_root else default_sessions_root()
    if not root.is_dir():
        return []
    paths = list(root.rglob("rollout-*.jsonl"))
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def observations(sessions_root: Path | str | None = None, *, cwd: str | Path | None = None,
                 surface: str | None = None, limit: int | None = None,
                 errors: list | None = None) -> list[RolloutObservation]:
    """Parsed observations, newest first, optionally filtered by cwd and surface.

    A rollout whose block cannot be parsed is **skipped, not fatal, and never
    silent**: pass `errors` to receive `(path, reason)` for each. `parse_block`
    fails closed when the vendor's intro wording changes, which is correct — but
    an older wording is common enough that one bad file must not abort a scan.
    On the capture machine **98 of 313** rollouts predate a change from "name,
    description, and file path" to "name, description, and source locator", and
    the first of them aborted a whole-history sweep.

    Counting the skips matters more than skipping quietly: a sweep that silently
    dropped a third of its input would report a confident, wrong history.
    """
    wanted_cwd = str(Path(cwd).resolve()) if cwd else None
    found: list[RolloutObservation] = []
    for path in find_rollouts(sessions_root):
        try:
            observation = read_rollout(path)
        except ValueError as exc:
            if errors is not None:
                errors.append((path, str(exc)))
            continue
        if observation is None:
            continue
        if surface is not None and observation.meta.surface != surface:
            continue
        if wanted_cwd is not None:
            try:
                if str(Path(observation.meta.cwd).resolve()) != wanted_cwd:
                    continue
            except OSError:
                continue
        found.append(observation)
        if limit is not None and len(found) >= limit:
            break
    return found


@dataclass
class LimitEvidence:
    """A budget limit and the basis it rests on.

    ``provisional`` is the load-bearing field. A limit taken from a vendor
    catalog and never checked against behaviour is provisional, and a provisional
    limit may not make a pair deployable — because that is exactly the mistake
    that made a 100%-of-budget target read as 74% for four days.
    """

    limit: int | None
    basis: str                       # "observed" | "vendor" | "indeterminate"
    provisional: bool
    reason: str = ""
    samples: tuple[tuple[int, int], ...] = ()   # (entry_count, charged_tokens)


def derive_limit(obs: list[RolloutObservation], *, clipped_only: bool = True) -> LimitEvidence:
    """Establish the limit by saturation, from renders that were actually clipped.

    Requires **two renders with different entry counts** whose charged totals
    agree exactly. Different counts matter: two renders of the same catalog agree
    trivially and disclose nothing. Only a clipped render saturates — an
    under-budget listing totals whatever it happens to cost, which is not a
    limit.
    """
    # **Never mix harness builds.** A 2026-07-10 session on build 0.144.1 charged
    # 6,188 tokens without clipping, while every 0.146.0 render saturates at
    # 4,000 — so the limit is a property of the build, not a constant. Pooling
    # them would either fabricate a disagreement or, worse, average two real
    # ceilings into one that belongs to neither.
    builds = {o.meta.harness_build for o in obs}
    if len(builds) > 1:
        return LimitEvidence(
            None, "indeterminate", True,
            f"samples span {len(builds)} harness builds ({', '.join(sorted(builds))}); "
            "a limit is a property of one build",
            (),
        )

    samples: list[tuple[int, int]] = []
    for observation in obs:
        if clipped_only and not is_saturated(observation):
            continue
        samples.append((len(observation.listing.entries), observation.charged_tokens))

    if len(samples) < 2:
        return LimitEvidence(None, "indeterminate", True,
                             f"need 2 saturating renders, have {len(samples)}",
                             tuple(samples))

    counts = {n for n, _ in samples}
    totals = {t for _, t in samples}
    if len(counts) < 2:
        return LimitEvidence(None, "indeterminate", True,
                             "all saturating renders have the same entry count, "
                             "so their agreement is trivial",
                             tuple(samples))
    if len(totals) != 1:
        return LimitEvidence(None, "indeterminate", True,
                             f"saturating renders disagree: {sorted(totals)}",
                             tuple(samples))
    return LimitEvidence(totals.pop(), "observed", False,
                         f"{len(samples)} saturating renders across "
                         f"{len(counts)} entry counts agree exactly",
                         tuple(samples))


def is_saturated(observation: RolloutObservation, source_descriptions: dict | None = None) -> bool:
    """Whether this render was clipped, i.e. spent its whole budget.

    Detected from the render itself: Codex's budget-driven clipping is uniform,
    so a saturated listing has many descriptions at an identical length that is
    shorter than the 1,024-character pre-cap. That pre-cap appends ``"..."``;
    budget clipping appends nothing, which is why the shape rather than a marker
    has to carry the signal.
    """
    lengths = [len(e.description or "") for e in observation.listing.entries]
    if len(lengths) < 3:
        return False
    if source_descriptions:
        return any(
            len(e.description or "") < len(source_descriptions.get(e.name, ""))
            for e in observation.listing.entries
        )
    longest = max(lengths)
    at_longest = sum(1 for length in lengths if abs(length - longest) <= 3)
    return longest < 1_000 and at_longest >= max(3, len(lengths) // 5)


def qualified_identities(listing: Listing) -> Counter:
    """`origin:name` per listed entry, with multiplicity preserved.

    A `Counter` rather than a set because Codex charges each copy of a
    duplicated name separately, so losing one copy is a real change.
    """
    return Counter(
        f"{classify_locator(_absolute(e.locator, listing.root_lines))}:{e.name}"
        for e in listing.entries
    )


def surface_mismatch(live: Listing, recorded: RolloutObservation) -> dict | None:
    """Report a live probe disagreeing with the recorded session.

    Returned rather than reconciled: this disagreement *is* the defect this
    module exists to catch, so it must be visible in evidence. Reconciling it
    silently would restore exactly the failure of the last four days.
    """
    # **A multiset of qualified identities, not a set of bare names.** Codex does
    # not shadow across roots: it lists every copy of a duplicated name and
    # charges for each. The 56-entry fixture carries two `skill-creator` entries
    # — one bundled by Codex, one shipped by dojo — so a set of names reports no
    # mismatch when one copy disappears, hiding a real cost difference.
    live_ids = qualified_identities(live)
    recorded_ids = qualified_identities(recorded.listing)
    if live_ids == recorded_ids:
        return None
    only_recorded = recorded_ids - live_ids     # Counter subtraction keeps multiplicity
    only_live = live_ids - recorded_ids
    return {
        "kind": "surface-mismatch",
        "live_surface": SURFACE_PROBE,
        "recorded_surface": recorded.meta.surface,
        "live_entries": sum(live_ids.values()),
        "recorded_entries": sum(recorded_ids.values()),
        "only_in_recorded": sorted(only_recorded.elements()),
        "only_in_live": sorted(only_live.elements()),
        "detail": (
            "the live probe renders a different code path than the recorded "
            "session; the recorded session is authoritative"
        ),
    }


def staleness(previous: RolloutObservation, current: RolloutObservation) -> list[str]:
    """Why a prior verdict no longer holds (SC-04, revision 14).

    Compares the uncontrolled entry **set**, not its size or cost. Both were
    unchanged on 2026-08-06 while six entries left and nine arrived.
    """
    reasons = []
    if previous.meta.harness_build != current.meta.harness_build:
        reasons.append(
            f"harness build changed: {previous.meta.harness_build} -> "
            f"{current.meta.harness_build}"
        )
    before, after = previous.uncontrolled_entries, current.uncontrolled_entries
    if before != after:
        gone, arrived = sorted(before - after), sorted(after - before)
        reasons.append(
            "uncontrolled entry set changed: "
            f"-{len(gone)} +{len(arrived)} ({', '.join(gone[:3] + arrived[:3])}…)"
        )
    return reasons


def attribute_demand(observation: RolloutObservation,
                     source_descriptions: dict[str, str] | None = None) -> dict[str, int]:
    """True demand per origin, from untruncated source where it is available.

    Where a source description is unavailable — a connector's payload, a bundled
    skill — the rendered text is a floor rather than a truth, and the caller is
    told so by `cost_basis` elsewhere. Costing those at zero understated a live
    target by 20%.
    """
    from .probe_codex import line_cost_tokens  # noqa: PLC0415 — avoids a cycle at import

    totals: dict[str, int] = {}
    descriptions = source_descriptions or {}
    for entry in observation.listing.entries:
        # Classify on the absolute locator, cost on the rendered one: Codex
        # charged the alias form, so costing the expansion would overstate.
        origin = observation.origin_of(entry.locator)
        text = descriptions.get(entry.name, entry.description or "")
        line = f"- {entry.name}: {text} ({entry.locator_kind}: {entry.locator})"
        totals[origin] = totals.get(origin, 0) + line_cost_tokens(line)
    if observation.listing.root_lines:
        totals["alias-table"] = observation.listing.root_table_cost_tokens
    return totals
