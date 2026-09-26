
""" """





















from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


APPROX_BYTES_PER_TOKEN = 4
SKILL_METADATA_CONTEXT_WINDOW_PERCENT = 2
DEFAULT_SKILL_METADATA_CHAR_BUDGET = 8_000
MAX_DEFAULT_CONTEXT_SKILL_DESCRIPTION_CHARS = 1_024
TRUNCATED_SKILL_DESCRIPTION_SUFFIX = "..."

BLOCK_OPEN = "<skills_instructions>"
BLOCK_CLOSE = "</skills_instructions>"
AVAILABLE_HEADER = "### Available skills"
ROOTS_HEADER = "### Skill roots"



INTRO_ABSOLUTE = "Each entry includes a name, description, and source locator."
INTRO_ALIASES = "a short path that can be expanded into an absolute path using the skill roots table"





ENTRY_RE = re.compile(r"^- (?P<body>.*?) \((?P<kind>[a-z ]+): (?P<locator>.*)\)$")

NAME_ONLY_RE = re.compile(r"^(?P<name>\S+):$")
NAME_DESC_RE = re.compile(r"^(?P<name>\S+): (?P<description>.*)$", re.DOTALL)


SKILLS_INTRO_WITH_ABSOLUTE_PATHS = (
    "A skill is a set of instructions provided through a `SKILL.md` source. Below is the list of "
    "skills that can be used. Each entry includes a name, description, and source locator. `file` "
    "locators are on the host filesystem, `environment resource` locators are owned by an execution "
    "environment, `orchestrator resource` locators are opaque non-filesystem resources, and `custom "
    "resource` locators use their provider's access mechanism."
)
SKILLS_INTRO_WITH_ALIASES = (
    "A skill is a set of local instructions to follow that is stored in a `SKILL.md` file. Below is "
    "the list of skills that can be used. Each entry includes a name, description, and a short path "
    "that can be expanded into an absolute path using the skill roots table."
)


def approx_tokens(text: str) -> int:
    """ """
    return (len(text.encode("utf-8")) + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN


def render_available_skills_body(root_lines: list[str], skill_lines: list[str]) -> str:
    """ """
    lines = ["## Skills"]
    if root_lines:
        lines.append(SKILLS_INTRO_WITH_ALIASES)
        lines.append(ROOTS_HEADER)
        lines.extend(root_lines)
    else:
        lines.append(SKILLS_INTRO_WITH_ABSOLUTE_PATHS)
    lines.append(AVAILABLE_HEADER)
    lines.extend(skill_lines)
    return "\n" + "\n".join(lines) + "\n"


def alias_table_cost_tokens(root_lines: list[str]) -> int:
    """ """





    if not root_lines:
        return 0
    return approx_tokens(render_available_skills_body(root_lines, [])) - approx_tokens(
        render_available_skills_body([], [])
    )


def line_cost_tokens(line: str) -> int:
    """ """




    return (len((line + "\n").encode("utf-8")) + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN


def budget_for_window(context_window: int | None) -> tuple[int, str]:
    """ """





    if context_window is not None and context_window > 0:
        return max(1, context_window * SKILL_METADATA_CONTEXT_WINDOW_PERCENT // 100), "tokens"
    return DEFAULT_SKILL_METADATA_CHAR_BUDGET, "characters"


@dataclass
class Entry:
    name: str
    description: str | None
    locator_kind: str
    locator: str
    rendered: str
    cost_tokens: int
    origin: str = "unknown"
    scope: str = "unknown"

    @property
    def is_namespaced(self) -> bool:
        """ """
        return ":" in self.name


@dataclass
class Listing:
    render_mode: str
    entries: list[Entry]
    root_lines: list[str]
    entry_cost_tokens: int
    root_table_cost_tokens: int
    block_chars: int
    warning: str | None = None
    fingerprint: dict = field(default_factory=dict)

    @property
    def charged_tokens(self) -> int:
        """ """





        return self.entry_cost_tokens + self.root_table_cost_tokens

    @property
    def charged_chars(self) -> int:
        """ """






        return sum(len(e.rendered) + 1 for e in self.entries) + sum(
            len(r) + 1 for r in self.root_lines
        )

    def utilization(self, limit: int, unit: str) -> float:
        """ """
        if unit == "characters":
            return self.charged_chars / limit
        if unit == "tokens":
            return self.charged_tokens / limit
        raise ValueError(f"unknown budget unit {unit!r}")


def _run(args: list[str], cwd: str | Path | None) -> str:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr[:400]}")
    return proc.stdout


def extract_block(prompt_input: list) -> str:
    """ """




    for message in prompt_input:
        for part in message.get("content", []) or []:
            text = part.get("text", "") if isinstance(part, dict) else ""
            if BLOCK_OPEN in text:
                start = text.index(BLOCK_OPEN)
                end = text.index(BLOCK_CLOSE) + len(BLOCK_CLOSE)
                return text[start:end]
    raise LookupError("no <skills_instructions> block in prompt-input")


def parse_block(block: str) -> Listing:
    """ """
    if INTRO_ALIASES in block:
        render_mode = "alias"
    elif INTRO_ABSOLUTE in block:
        render_mode = "absolute"
    else:
        raise ValueError("block matches neither known intro; render.rs may have changed")

    lines = block.splitlines()
    entries: list[Entry] = []
    root_lines: list[str] = []
    section = None

    for line in lines:
        if line.startswith(AVAILABLE_HEADER):
            section = "skills"
            continue
        if line.startswith(ROOTS_HEADER):
            section = "roots"
            continue
        if line.startswith("###") or line.startswith("## "):
            section = None
            continue

        if section == "roots" and line.startswith("- "):
            root_lines.append(line)
            continue
        if section != "skills" or not line.startswith("- "):
            continue

        match = ENTRY_RE.match(line)
        if not match:



            continue

        body = match.group("body")
        name_only = NAME_ONLY_RE.match(body)
        if name_only:
            name, description = name_only.group("name"), None
        else:
            name_desc = NAME_DESC_RE.match(body)
            if not name_desc:
                continue
            name, description = name_desc.group("name"), name_desc.group("description")

        entries.append(
            Entry(
                name=name,
                description=description,
                locator_kind=match.group("kind"),
                locator=match.group("locator"),
                rendered=line,
                cost_tokens=line_cost_tokens(line),
            )
        )

    return Listing(
        render_mode=render_mode,
        entries=entries,
        root_lines=root_lines,
        entry_cost_tokens=sum(e.cost_tokens for e in entries),
        root_table_cost_tokens=alias_table_cost_tokens(root_lines),
        block_chars=len(block),
        warning=_find_warning(block),
    )


def _find_warning(block: str) -> str | None:
    """ """
    for marker in (
        "Exceeded skills context budget",
        "Skill descriptions were shortened",
    ):
        index = block.find(marker)
        if index != -1:
            return block[index : block.find("\n", index) if block.find("\n", index) != -1 else None]
    return None


def models(cwd: str | Path | None = None) -> list[dict]:
    """ """







    return json.loads(_run(["codex", "debug", "models"], cwd)).get("models", [])


def active_model(codex_home: Path | None = None) -> str | None:
    """ """





    home = codex_home or (Path.home() / ".codex")
    config = home / "config.toml"
    if not config.exists():
        return None
    for line in config.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            break
        match = re.match(r'^model\s*=\s*"([^"]+)"', stripped)
        if match:
            return match.group(1)
    return None


def probe(cwd: str | Path | None = None, dojo_skills_root: Path | None = None) -> Listing:
    """ """






    raw = _run(["codex", "debug", "prompt-input"], cwd)
    listing = parse_block(extract_block(json.loads(raw)))
    listing.fingerprint = fingerprint(cwd)
    root = dojo_skills_root or (Path(__file__).resolve().parents[2] / "skills")
    return classify(listing, root, None, Path(cwd) if cwd else None)


def fingerprint(cwd: str | Path | None = None) -> dict:
    """ """
    version = _run(["codex", "--version"], cwd).strip()
    catalog = models(cwd)
    slug = active_model()
    entry = next((m for m in catalog if m.get("slug") == slug), None)
    if entry is None:



        windows = {m.get("context_window") for m in catalog if m.get("context_window")}
        window = windows.pop() if len(windows) == 1 else None
        resolution = "catalog-unanimous" if window else "indeterminate"
    else:
        window = entry.get("context_window")
        resolution = "configured"

    limit, unit = budget_for_window(window)
    return {
        "harness": "codex",
        "version": version,
        "model": slug,
        "model_resolution": resolution,
        "context_window": window,
        "effective_context_window_percent": (entry or {}).get("effective_context_window_percent"),
        "budget_limit": limit,
        "budget_unit": unit,
    }


def is_stale(recorded: dict, current: dict) -> list[str]:
    """ """





    keys = set(recorded) | set(current)
    return sorted(k for k in keys if recorded.get(k) != current.get(k))


CODEX_HOME_RE = re.compile(r"^(?P<home>.*/\.codex)/")


def infer_codex_home(listing: Listing) -> str | None:
    """ """











    for entry in listing.entries:
        locator = _absolute(entry.locator, listing.root_lines)
        if match := CODEX_HOME_RE.match(locator):
            return match.group("home")
    for line in listing.root_lines:
        if match := CODEX_HOME_RE.match(re.sub(r"^- `\w+` = `|`$", "", line) + "/"):
            return match.group("home")
    return None


def classify(
    listing: Listing,
    dojo_skills_root: Path,
    codex_home: Path | str | None = None,
    cwd: Path | None = None,
) -> Listing:
    """ """







    canonical = {p.name for p in dojo_skills_root.iterdir() if p.is_dir() and not p.name.startswith("_")}
    home = str(codex_home) if codex_home else infer_codex_home(listing)
    if home is None:
        raise ValueError(
            "cannot determine the Codex home for this listing; refusing to classify, "
            "because every origin would silently fall through to dojo-managed or foreign"
        )
    plugin_cache = f"{home}/plugins/"
    system_root = f"{home}/skills/.system/"











    project_root = None
    if cwd:
        candidate = Path(cwd) / ".agents" / "skills"
        if candidate.is_symlink() or candidate.is_dir():
            project_root = f"{candidate.resolve()}/"

    for entry in listing.entries:
        locator = _absolute(entry.locator, listing.root_lines)

        if entry.locator_kind != "file":
            entry.origin = "harness-bundled"
        elif plugin_cache in locator:
            entry.origin = "plugin"
        elif system_root in locator or "codex-primary-runtime" in locator:
            entry.origin = "harness-bundled"
        elif ":" not in entry.name and entry.name in canonical:
            entry.origin = "dojo-managed"
        else:
            entry.origin = "foreign"

        entry.scope = "project" if project_root and locator.startswith(project_root) else "user"
    return listing


def _absolute(locator: str, root_lines: list[str]) -> str:
    """ """




    if locator.startswith("/"):
        return locator
    for line in root_lines:
        match = re.match(r"^- `(?P<alias>\w+)` = `(?P<path>.*)`$", line)
        if match and locator.startswith(match.group("alias") + "/"):
            return match.group("path") + locator[len(match.group("alias")) :]
    return locator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".", help="working directory to probe")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument("--from-fixture", help="parse a captured prompt-input JSON instead of probing")
    args = parser.parse_args(argv)

    if args.from_fixture:
        listing = parse_block(extract_block(json.loads(Path(args.from_fixture).read_text())))
    else:
        listing = probe(args.cwd)

    if args.json:
        payload = asdict(listing)
        payload["charged_tokens"] = listing.charged_tokens
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        limit = listing.fingerprint.get("budget_limit") or 0
        unit = listing.fingerprint.get("budget_unit") or ""
        origins = collections.Counter(e.origin for e in listing.entries)
        print(f"render mode      : {listing.render_mode}")
        print(f"entries          : {len(listing.entries)}")
        print(f"namespaced       : {sum(1 for e in listing.entries if e.is_namespaced)}")
        print(f"origins          : {dict(origins)}")
        print(f"charged tokens   : {listing.charged_tokens}")
        print(f"charged chars    : {listing.charged_chars}")
        print(f"block chars      : {listing.block_chars}")
        if limit:
            pct = 100 * listing.utilization(limit, unit)
            print(f"budget           : {limit} {unit} ({pct:.1f}%)")
            if listing.fingerprint.get("model_resolution") == "indeterminate":
                print("                   ^ UNSUPPORTED: no context window resolved, "
                      "character fallback in force; do not treat as a deployable verdict")
        if listing.warning:
            print(f"warning          : {listing.warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
