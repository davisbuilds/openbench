
""" """







































from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .probe_codex import Listing, _absolute, parse_block

BLOCK_OPEN = "<skills_instructions>"
BLOCK_CLOSE = "</skills_instructions>"

SURFACE_TUI = "codex-tui"
SURFACE_EXEC = "codex_exec"
SURFACE_PROBE = "exec"

_BLOCK_RE = re.compile(rf"{BLOCK_OPEN}.*?{BLOCK_CLOSE}", re.S)





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







UNCONTROLLED_ORIGINS = frozenset({ORIGIN_CONNECTOR, ORIGIN_PLUGIN, ORIGIN_BUNDLED})


@dataclass(frozen=True)
class RolloutMeta:
    """ """

    path: Path
    surface: str
    cli_version: str
    cwd: str
    model: str | None
    session_id: str | None = None

    @property
    def harness_build(self) -> str:
        """ """





        return f"{self.cli_version}/{self.model or 'unknown-model'}"


@dataclass
class RolloutObservation:
    """ """

    meta: RolloutMeta
    listing: Listing

    @property
    def charged_tokens(self) -> int:
        return self.listing.charged_tokens

    @property
    def entry_names(self) -> frozenset[str]:
        return frozenset(e.name for e in self.listing.entries)

    def absolute_locator(self, locator: str) -> str:
        """ """







        return _absolute(locator, self.listing.root_lines)

    def origin_of(self, locator: str) -> str:
        return classify_locator(self.absolute_locator(locator))

    @property
    def uncontrolled_entries(self) -> frozenset[str]:
        """ """















        return frozenset(
            f"{origin}:{e.name}"
            for e in self.listing.entries
            for origin in (self.origin_of(e.locator),)
            if origin in UNCONTROLLED_ORIGINS
        )


def classify_locator(locator: str) -> str:
    """ """
    if CONNECTOR_SEGMENT in locator:
        return ORIGIN_CONNECTOR
    if DOJO_SEGMENT in locator:
        return ORIGIN_DOJO
    if any(seg in locator for seg in BUNDLED_SEGMENTS):
        return ORIGIN_BUNDLED
    if PLUGIN_CACHE_SEGMENT in locator:


        probe = Path(locator)
        for parent in list(probe.parents)[:6]:
            if (parent / REMOTE_MARKER).exists():
                return ORIGIN_CONNECTOR
        return ORIGIN_PLUGIN
    return ORIGIN_FOREIGN


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
    """ """





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
    """ """





    root = Path(sessions_root) if sessions_root else default_sessions_root()
    if not root.is_dir():
        return []
    paths = list(root.rglob("rollout-*.jsonl"))
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def observations(sessions_root: Path | str | None = None, *, cwd: str | Path | None = None,
                 surface: str | None = None, limit: int | None = None,
                 errors: list | None = None) -> list[RolloutObservation]:
    """ """












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
    """ """







    limit: int | None
    basis: str
    provisional: bool
    reason: str = ""
    samples: tuple[tuple[int, int], ...] = ()


def derive_limit(obs: list[RolloutObservation], *, clipped_only: bool = True) -> LimitEvidence:
    """ """












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
    """ """







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


def surface_mismatch(live: Listing, recorded: RolloutObservation) -> dict | None:
    """ """





    live_names = frozenset(e.name for e in live.entries)
    recorded_names = recorded.entry_names
    if live_names == recorded_names:
        return None
    return {
        "kind": "surface-mismatch",
        "live_surface": SURFACE_PROBE,
        "recorded_surface": recorded.meta.surface,
        "live_entries": len(live_names),
        "recorded_entries": len(recorded_names),
        "only_in_recorded": sorted(recorded_names - live_names),
        "only_in_live": sorted(live_names - recorded_names),
        "detail": (
            "the live probe renders a different code path than the recorded "
            "session; the recorded session is authoritative"
        ),
    }


def staleness(previous: RolloutObservation, current: RolloutObservation) -> list[str]:
    """ """




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
    """ """






    from .probe_codex import line_cost_tokens

    totals: dict[str, int] = {}
    descriptions = source_descriptions or {}
    for entry in observation.listing.entries:


        origin = observation.origin_of(entry.locator)
        text = descriptions.get(entry.name, entry.description or "")
        line = f"- {entry.name}: {text} ({entry.locator_kind}: {entry.locator})"
        totals[origin] = totals.get(origin, 0) + line_cost_tokens(line)
    if observation.listing.root_lines:
        totals["alias-table"] = observation.listing.root_table_cost_tokens
    return totals
