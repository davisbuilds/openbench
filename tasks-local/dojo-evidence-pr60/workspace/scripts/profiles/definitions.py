
""" """






























from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml





BASELINE = "core"
INSPECTION = "full"
OVERLAYS = ("design", "engineering", "knowledge", "research", "shipping", "skill-authoring")
VOCABULARY = frozenset({BASELINE, INSPECTION, *OVERLAYS})



KIND_BY_NAME = {BASELINE: "baseline", INSPECTION: "inspection", **{o: "overlay" for o in OVERLAYS}}
KINDS = frozenset(KIND_BY_NAME.values())



ANCHORS = {
    "design": ("design-critique", "web-design-guidelines"),
    "engineering": ("create-cli", "secure-code"),
    "knowledge": ("obsidian-markdown", "session-retro"),
    "research": ("deep-research", "research-architect"),
    "shipping": ("gh-commit-push-pr", "vercel-deploy"),
    "skill-authoring": ("skill-creator", "skill-standardizer"),
}









CORE_MEMBERS = (
    "brainstorming",
    "diagnose",
    "first-principles",
    "local-review",
    "test-strategy",
    "verify-before-complete",
    "write-plan",
    "write-spec",
)

MIN_NON_CORE_MEMBERS = 2
SENTINEL = "*"
REQUIRED_PROFILE_KEYS = ("name", "kind", "description", "members")
EQUIVALENCE_FILENAME = "harness-equivalences.yaml"
REQUIRED_EQUIVALENCE_KEYS = ("skill", "harness", "bundled_entry", "evidence")





SUPPORTED_HARNESSES = ("claude-code", "codex")


class ProfileDefinitionError(ValueError):
    """ """







class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """ """


def _no_duplicate_keys(loader: _NoDuplicateKeyLoader, node: yaml.MappingNode) -> dict:
    seen: set = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise ProfileDefinitionError(f"duplicate key {key!r} in mapping at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=True)


_NoDuplicateKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def _load_yaml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileDefinitionError(f"{path.name}: cannot be read ({exc})") from exc
    try:
        data = yaml.load(text, Loader=_NoDuplicateKeyLoader)
    except ProfileDefinitionError as exc:
        raise ProfileDefinitionError(f"{path.name}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProfileDefinitionError(f"{path.name}: is not readable YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise ProfileDefinitionError(f"{path.name}: must be a YAML mapping, got {type(data).__name__}")
    return data


@dataclass(frozen=True)
class Profile:
    """ """







    name: str
    kind: str
    description: str
    members: tuple[str, ...]
    is_sentinel: bool
    source: Path


def load_catalog(path: Path | str) -> dict[str, dict]:
    """ """






    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileDefinitionError(f"cannot read canonical catalog {path}: {exc}") from exc
    skills = data.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ProfileDefinitionError(f"{path}: holds no skills; refusing to validate membership against an empty catalog")
    catalog = {}
    for entry in skills:
        name = entry.get("name")
        if not name:
            raise ProfileDefinitionError(f"{path}: catalog entry without a name: {entry!r}")
        if name in catalog:
            raise ProfileDefinitionError(f"{path}: duplicate catalog entry {name!r}")
        catalog[name] = entry
    return catalog


def _parse_profile(path: Path) -> Profile:
    """ """
    data = _load_yaml(path)

    missing = [k for k in REQUIRED_PROFILE_KEYS if k not in data]
    if missing:
        raise ProfileDefinitionError(f"{path.name}: missing required key(s) {', '.join(sorted(missing))}")
    unknown = sorted(set(data) - set(REQUIRED_PROFILE_KEYS))
    if unknown:
        raise ProfileDefinitionError(f"{path.name}: unknown key(s) {', '.join(unknown)}")

    name, kind, description, raw_members = (data[k] for k in REQUIRED_PROFILE_KEYS)

    if not isinstance(name, str) or not name.strip():
        raise ProfileDefinitionError(f"{path.name}: `name` must be a non-empty string")
    if kind not in KINDS:
        raise ProfileDefinitionError(
            f"{path.name}: profile {name!r} declares unknown kind {kind!r}; expected one of {', '.join(sorted(KINDS))}"
        )
    if not isinstance(description, str) or not description.strip():
        raise ProfileDefinitionError(
            f"{path.name}: profile {name!r} has an empty `description`; these files are reviewed data "
            "and a definition that does not say why it exists cannot be challenged"
        )

    
    
    
    if raw_members == SENTINEL:
        if name != INSPECTION:
            raise ProfileDefinitionError(
                f"{path.name}: profile {name!r} uses the whole-catalog sentinel {SENTINEL!r}; "
                f"only {INSPECTION!r} may"
            )
        return Profile(name, kind, description, (), True, path)
    if name == INSPECTION:
        raise ProfileDefinitionError(
            f"{path.name}: profile {INSPECTION!r} must declare members: {SENTINEL!r}, not a pinned list; "
            "a pinned list goes stale the moment a skill is authored"
        )

    if not isinstance(raw_members, list) or not raw_members:
        raise ProfileDefinitionError(
            f"{path.name}: profile {name!r} declares no members; expected a non-empty list of "
            f"canonical skill names, got {type(raw_members).__name__}"
        )
    if not all(isinstance(m, str) and m.strip() for m in raw_members):
        raise ProfileDefinitionError(f"{path.name}: profile {name!r} has a non-string member entry")
    duplicates = sorted({m for m in raw_members if raw_members.count(m) > 1})
    if duplicates:
        raise ProfileDefinitionError(
            f"{path.name}: profile {name!r} lists member(s) {', '.join(duplicates)} more than once"
        )
    return Profile(name, kind, description, tuple(sorted(raw_members)), False, path)


def load_definitions(profiles_dir: Path | str, catalog: dict[str, dict] | None = None) -> dict[str, Profile]:
    """ """





    profiles_dir = Path(profiles_dir)
    if catalog is None:
        catalog = load_catalog(profiles_dir.parent / "skills.json")

    paths = sorted(p for p in profiles_dir.glob("*.yaml") if p.name != EQUIVALENCE_FILENAME)
    if not paths:
        raise ProfileDefinitionError(f"{profiles_dir}: holds no profile definitions")

    by_name: dict[str, Profile] = {}
    for path in paths:
        profile = _parse_profile(path)
        if profile.name in by_name:
            raise ProfileDefinitionError(
                f"duplicate profile definition {profile.name!r}: declared in "
                f"{by_name[profile.name].source.name} and {path.name}"
            )
        by_name[profile.name] = profile

    _validate_vocabulary(by_name, profiles_dir)
    for name in sorted(by_name):
        _validate_membership(by_name[name], by_name[BASELINE], catalog)
    return {name: by_name[name] for name in sorted(by_name)}


def _validate_vocabulary(by_name: dict[str, Profile], profiles_dir: Path) -> None:
    """ """
    unknown = sorted(set(by_name) - VOCABULARY)
    if unknown:
        raise ProfileDefinitionError(
            f"{profiles_dir}: profile(s) {', '.join(unknown)} are outside the SC-02 vocabulary "
            f"({', '.join(sorted(VOCABULARY))}); adding one is a contract revision"
        )
    absent = sorted(VOCABULARY - set(by_name))
    if absent:
        raise ProfileDefinitionError(
            f"{profiles_dir}: SC-02 profile(s) {', '.join(absent)} have no definition file"
        )
    for name, profile in sorted(by_name.items()):
        if profile.kind != KIND_BY_NAME[name]:
            raise ProfileDefinitionError(
                f"{profile.source.name}: profile {name!r} declares kind {profile.kind!r}, "
                f"but SC-02 makes it {KIND_BY_NAME[name]!r}"
            )


def _validate_membership(profile: Profile, core: Profile, catalog: dict[str, dict]) -> None:
    """ """
    unknown = [m for m in profile.members if m not in catalog]
    if unknown:
        raise ProfileDefinitionError(
            f"{profile.source.name}: profile {profile.name!r} names member(s) "
            f"{', '.join(sorted(unknown))} absent from the canonical catalog"
        )
    if profile.name == BASELINE and set(profile.members) != set(CORE_MEMBERS):
        missing = sorted(set(CORE_MEMBERS) - set(profile.members))
        extra = sorted(set(profile.members) - set(CORE_MEMBERS))
        raise ProfileDefinitionError(
            f"{profile.source.name}: profile {BASELINE!r} must be exactly the SC-03 set; "
            f"missing {', '.join(missing) or 'none'}; unexpected {', '.join(extra) or 'none'}"
        )
    if profile.kind != "overlay":
        return

    
    
    non_core = sorted(set(profile.members) - set(core.members))
    if len(non_core) < MIN_NON_CORE_MEMBERS:
        raise ProfileDefinitionError(
            f"{profile.source.name}: overlay {profile.name!r} adds {len(non_core)} non-{BASELINE!r} "
            f"member(s) ({', '.join(non_core) or 'none'}); SC-02 requires at least {MIN_NON_CORE_MEMBERS}"
        )
    missing_anchors = [a for a in ANCHORS[profile.name] if a not in profile.members]
    if missing_anchors:
        raise ProfileDefinitionError(
            f"{profile.source.name}: overlay {profile.name!r} is missing required SC-02 anchor(s) "
            f"{', '.join(sorted(missing_anchors))}"
        )


def resolved_members(profile: Profile, catalog: dict[str, dict]) -> tuple[str, ...]:
    """ """




    if profile.is_sentinel:
        return tuple(sorted(catalog))
    return profile.members


@dataclass(frozen=True)
class Equivalence:
    """ """

    skill: str
    harness: str
    bundled_entry: str
    evidence: str


@dataclass(frozen=True)
class Equivalences:
    """ """






    entries: tuple[Equivalence, ...]
    identity: str
    source: Path

    def for_harness(self, harness: str) -> dict[str, Equivalence]:
        """ """
        if harness not in SUPPORTED_HARNESSES:
            raise ProfileDefinitionError(
                f"unknown harness {harness!r}; supported: {', '.join(SUPPORTED_HARNESSES)}"
            )
        return {e.skill: e for e in self.entries if e.harness == harness}


def equivalence_identity(entries: tuple[Equivalence, ...]) -> str:
    """ """





    payload = sorted(
        [e.harness, e.skill, e.bundled_entry, " ".join(e.evidence.split())] for e in entries
    )
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_equivalences(path: Path | str, catalog: dict[str, dict]) -> Equivalences:
    """ """





    path = Path(path)
    data = _load_yaml(path)

    raw = data.get("equivalences")
    if raw is None or not isinstance(raw, list):
        raise ProfileDefinitionError(f"{path.name}: must declare a list under `equivalences`")

    entries: list[Equivalence] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ProfileDefinitionError(f"{path.name}: equivalence entries must be mappings, got {item!r}")
        missing = [k for k in REQUIRED_EQUIVALENCE_KEYS if not str(item.get(k, "")).strip()]
        if missing:
            raise ProfileDefinitionError(
                f"{path.name}: equivalence for skill {item.get('skill', '<unnamed>')!r} on harness "
                f"{item.get('harness', '<unnamed>')!r} is missing {', '.join(sorted(missing))}"
            )
        skill, harness = item["skill"], item["harness"]
        if harness not in SUPPORTED_HARNESSES:
            raise ProfileDefinitionError(
                f"{path.name}: equivalence for skill {skill!r} names unknown harness {harness!r}; "
                f"supported: {', '.join(SUPPORTED_HARNESSES)}"
            )
        if skill not in catalog:
            raise ProfileDefinitionError(
                f"{path.name}: equivalence on harness {harness!r} names skill {skill!r}, which is absent "
                "from the canonical catalog; declaring one for a skill dojo does not ship hides a future "
                "collision rather than resolving one"
            )
        if (skill, harness) in seen:
            raise ProfileDefinitionError(
                f"{path.name}: duplicate equivalence for ({skill!r}, {harness!r})"
            )
        seen.add((skill, harness))
        entries.append(Equivalence(skill, harness, item["bundled_entry"], item["evidence"]))

    for skill in sorted({e.skill for e in entries}):
        declared = {e.harness for e in entries if e.skill == skill}
        if declared >= set(SUPPORTED_HARNESSES):
            raise ProfileDefinitionError(
                f"{path.name}: skill {skill!r} is declared equivalent on every supported harness "
                f"({', '.join(sorted(declared))}), which resolves to an empty realization everywhere; "
                "that is a profile-definition error, not a resolution"
            )

    ordered = tuple(sorted(entries, key=lambda e: (e.harness, e.skill)))
    return Equivalences(ordered, equivalence_identity(ordered), path)


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Validate and print the profile definitions.")
    parser.add_argument("--profiles-dir", default=str(repo_root / "profiles"))
    parser.add_argument("--catalog", default=str(repo_root / "skills.json"))
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    profiles = load_definitions(args.profiles_dir, catalog)
    equivalences = load_equivalences(Path(args.profiles_dir) / EQUIVALENCE_FILENAME, catalog)

    if args.json:
        json.dump(
            {
                "profiles": {
                    name: {
                        "kind": p.kind,
                        "members": list(resolved_members(p, catalog)),
                        "sentinel": p.is_sentinel,
                    }
                    for name, p in profiles.items()
                },
                "equivalence_identity": equivalences.identity,
                "equivalences": [
                    {"skill": e.skill, "harness": e.harness, "bundled_entry": e.bundled_entry}
                    for e in equivalences.entries
                ],
            },
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
    else:
        for name, profile in profiles.items():
            members = resolved_members(profile, catalog)
            print(f"{name:16s} {profile.kind:11s} {len(members):3d} members")
            print(f"                 {', '.join(members)}")
        print(f"\nequivalence identity: {equivalences.identity}")
        for entry in equivalences.entries:
            print(f"  {entry.harness}: {entry.skill} -> {entry.bundled_entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
