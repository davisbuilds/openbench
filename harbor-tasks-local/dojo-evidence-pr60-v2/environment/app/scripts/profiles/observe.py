
""" """





















from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .budget import Policy
from .probe_codex import Listing, _absolute

REPO_ROOT = Path(__file__).resolve().parents[2]
_STANDARDIZER = REPO_ROOT / "skills" / "skill-standardizer" / "scripts"












PLUGIN_CACHE = {
    "codex": "/.codex/plugins/",
    "claude-code": "/.claude/plugins/",
}


@dataclass
class ObservedEntry:
    """ """

    name: str
    origin: str
    scope: str
    locator: str
    source_description: str | None = None
    listed_description: str | None = None
    cost: int = 0
    is_symlink: bool | None = None
    link_target: str | None = None
    dir_hash: str | None = None
    duplicate_of: str | None = None
    exempt: bool = False




    observed: bool = True


@dataclass
class Observation:
    """ """

    harness: str
    entries: list[ObservedEntry] = field(default_factory=list)
    root_lines: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    warning: str | None = None




    symlinked_scope_roots: list[tuple[str, str]] = field(default_factory=list)

    @property
    def duplicated_names(self) -> tuple[str, ...]:
        seen, dupes = set(), set()
        for entry in self.entries:
            if entry.name in seen:
                dupes.add(entry.name)
            seen.add(entry.name)
        return tuple(sorted(dupes))

    def as_budget_entries(self) -> list[dict]:
        """ """












        entries = []
        for e in self.entries:
            basis = "source" if e.source_description is not None else "observed"
            entries.append(
                {
                    "name": e.name,
                    "source_description": e.source_description
                    if e.source_description is not None
                    else (e.listed_description or ""),
                    "cost_basis": basis,
                    **({"listed_description": e.listed_description} if e.observed else {}),
                    "locator": e.locator,
                    "exempt": e.exempt,
                }
            )
        return entries

    @property
    def cost_basis_counts(self) -> dict[str, int]:
        """ """





        counts = {"source": 0, "observed": 0}
        for e in self.entries:
            counts["source" if e.source_description is not None else "observed"] += 1
        return counts



def _standardizer():
    """ """




    if str(_STANDARDIZER) not in sys.path:
        sys.path.insert(0, str(_STANDARDIZER))
    import skill_standardizer_lib

    return skill_standardizer_lib


def source_descriptions(skills_root: Path) -> dict[str, str]:
    """ """













    out: dict[str, str] = {}
    for path in sorted(skills_root.iterdir()):
        skill_md = path / "SKILL.md"
        if not path.is_dir() or path.name.startswith(("_", ".")) or not skill_md.exists():
            continue
        parts = skill_md.read_text(encoding="utf-8").split("---")
        if len(parts) < 2:
            continue
        try:
            frontmatter = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            continue
        description = frontmatter.get("description")
        if isinstance(description, str) and description.strip():
            out[path.name] = description.strip()
    return out


def observe_codex(listing: Listing, policy: Policy, skills_root: Path,
                  cwd: Path | None = None) -> Observation:
    """ """





    if policy.harness != "codex":
        raise ValueError(f"expected a codex policy, got {policy.harness!r}")

    descriptions = source_descriptions(skills_root)
    observation = Observation(harness=policy.harness, root_lines=list(listing.root_lines),
                              warning=listing.warning)

    seen: dict[str, str] = {}
    for entry in listing.entries:
        if entry.origin == "unknown":
            raise ValueError(
                f"entry {entry.name!r} is unclassified; classify the listing before observing it, "
                "or every origin silently collapses into one bucket"
            )
        observed = ObservedEntry(
            name=entry.name,
            origin=entry.origin,
            scope=entry.scope,
            locator=_absolute(entry.locator, listing.root_lines),
            listed_description=entry.description,








            source_description=descriptions.get(entry.name) if entry.origin == "dojo-managed" else None,
            cost=entry.cost_tokens,
            exempt=entry.origin == "harness-bundled",
        )


        if entry.name in seen and not policy.shadows_by_name:
            observed.duplicate_of = seen[entry.name]
        seen.setdefault(entry.name, observed.locator)
        observation.entries.append(observed)

    if cwd is not None:
        candidate = Path(cwd) / policy.project_scope_root
        if candidate.is_symlink():
            observation.symlinked_scope_roots.append((str(candidate), str(candidate.resolve())))

    _attach_topology(observation, skills_root)
    return observation


def observe_claude(result, debug, policy: Policy, skills_root: Path) -> Observation:
    """ """






    if policy.harness != "claude-code":
        raise ValueError(f"expected a claude-code policy, got {policy.harness!r}")

    descriptions = source_descriptions(skills_root)
    observation = Observation(harness=policy.harness)

    for entry in result.entries:
        observation.entries.append(
            ObservedEntry(
                name=entry.name,
                origin=entry.origin,
                scope=entry.scope,
                locator="",
                listed_description=entry.description,
                source_description=descriptions.get(entry.name),
                exempt=entry.origin in ("harness-bundled", "unresolved"),
            )
        )

    if debug is not None and debug.sent is not None and debug.sent != len(observation.entries):
        observation.unsupported.append(
            f"debug reports {debug.sent} sent but {len(observation.entries)} entries parsed"
        )
    if debug is not None:
        observation.warning = "over budget" if debug.over_budget else None
    return observation


def _attach_topology(observation: Observation, skills_root: Path) -> None:
    """ """




    lib = _standardizer()
    for entry in observation.entries:
        if entry.origin != "dojo-managed" or not entry.locator:
            continue
        directory = Path(entry.locator).parent
        if not directory.is_dir():
            continue
        entry.is_symlink = directory.is_symlink()
        if entry.is_symlink:
            entry.link_target = str(Path(directory).readlink())
        try:
            entry.dir_hash = lib.hash_directory(directory)
        except Exception:
            entry.dir_hash = None
