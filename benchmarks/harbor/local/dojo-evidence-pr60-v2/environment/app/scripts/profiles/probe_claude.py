
""" """


























from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


SKILL_LISTING_BUDGET_FRACTION = 0.01
SKILL_LISTING_MAX_DESC_CHARS = 1536
BYTES_PER_TOKEN = 4
DEFAULT_CONTEXT_TOKENS = 200_000

LISTING_OPENING = "The following skills are available for use with the Skill tool:"


DECOY_OPENING = "## Available Skills"

LOADED_RE = re.compile(r"Loaded (\d+) unique skills \((.*?)\)")
SENDING_RE = re.compile(r"Sending (\d+) skills via attachment")
BUDGET_RE = re.compile(r"Skill listing over budget: (\d+) skills, (\d+) chars > (\d+) budget")
SOURCES_RE = re.compile(r"Loading skills from: (.*)")










ENTRY_RE = re.compile(r"^- (?P<name>.+?)(?:: (?P<description>.*))?$")


def budget_chars(context_tokens: int = DEFAULT_CONTEXT_TOKENS, fraction: float = SKILL_LISTING_BUDGET_FRACTION) -> int:
    """ """




    return int(context_tokens * BYTES_PER_TOKEN * fraction)


@dataclass
class Entry:
    name: str
    description: str | None
    shape: str
    origin: str = "unknown"
    scope: str = "unknown"

    @property
    def is_namespaced(self) -> bool:
        """ """
        return ":" in self.name


@dataclass
class DebugResult:
    """ """

    loaded: int | None = None
    sent: int | None = None
    demand_chars: int | None = None
    budget_chars: int | None = None
    over_budget: bool = False
    sources: str | None = None





    warned_skills: int | None = None
    fingerprint: dict = field(default_factory=dict)

    @property
    def ratio(self) -> float | None:
        if self.demand_chars and self.budget_chars:
            return self.demand_chars / self.budget_chars
        return None

    @property
    def counts_disagree(self) -> bool:
        return (
            self.warned_skills is not None
            and self.sent is not None
            and self.warned_skills != self.sent
        )


@dataclass
class RequestResult:
    """ """

    model: str | None
    entries: list[Entry]
    rendered_chars: int

    @property
    def description_removed(self) -> int:
        return sum(1 for e in self.entries if e.shape == "description_removed")

    @property
    def ellipsis_truncated(self) -> int:
        return sum(1 for e in self.entries if e.shape == "ellipsis_truncated")


def _claude(args: list[str], cwd: str | Path | None, env: dict | None = None) -> None:
    full_env = {**os.environ, **(env or {})}
    subprocess.run(
        ["claude", "-p", "--no-session-persistence", *args, "say ok"],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
    )


def parse_debug(text: str) -> DebugResult:
    """ """
    result = DebugResult()
    if match := LOADED_RE.search(text):
        result.loaded = int(match.group(1))
    if match := SENDING_RE.search(text):
        result.sent = int(match.group(1))
    if match := SOURCES_RE.search(text):
        result.sources = match.group(1).strip()
    if match := BUDGET_RE.search(text):
        result.warned_skills = int(match.group(1))
        result.demand_chars = int(match.group(2))
        result.budget_chars = int(match.group(3))
        result.over_budget = True
    return result


def debug(cwd: str | Path | None = None, model: str = "haiku") -> DebugResult:
    """ """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "debug.txt"
        _claude(["--model", model, "--debug-file", str(path)], cwd)
        text = path.read_text() if path.exists() else ""
    result = parse_debug(text)
    result.fingerprint = fingerprint(model)
    return result


def find_listing(body: dict) -> str | None:
    """ """







    def walk(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    for text in walk(body):
        if LISTING_OPENING in text:
            start = text.index(LISTING_OPENING)
            end = text.find("</system-reminder>", start)
            return text[start : end if end != -1 else len(text)]
    return None


def parse_listing(block: str) -> list[Entry]:
    """ """
    entries: list[Entry] = []
    for line in block.splitlines():
        if not line.startswith("- "):
            continue
        match = ENTRY_RE.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        description = match.group("description")
        if description is None:
            shape = "description_removed"
        elif description.rstrip().endswith("…"):
            shape = "ellipsis_truncated"
        else:
            shape = "full"
        entries.append(Entry(name=name, description=description, shape=shape))
    return entries


def classify(
    result: RequestResult,
    dojo_skills_root: Path,
    project_root: Path | None = None,
    user_root: Path | None = None,
) -> RequestResult:
    """ """














    canonical = {
        p.name for p in dojo_skills_root.iterdir() if p.is_dir() and not p.name.startswith("_")
    }
    project = _names_in(project_root)
    user = _names_in(user_root if user_root is not None else Path.home() / ".claude" / "skills")

    for entry in result.entries:
        if entry.is_namespaced:
            entry.origin = "plugin"
        elif entry.name in canonical and (entry.name in project or entry.name in user):
            entry.origin = "dojo-managed"
        elif entry.name in project or entry.name in user:
            entry.origin = "foreign"
        else:










            entry.origin = "unresolved"



        entry.scope = "project" if entry.name in project else "user" if entry.name in user else "bundled"
    return result


def _names_in(root: Path | None) -> set[str]:
    if root is None or not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}


def parse_request(body: dict) -> RequestResult:
    block = find_listing(body)
    if block is None:
        raise LookupError("no skills listing in request body")
    entries = parse_listing(block)
    raw = sum(1 for line in block.splitlines() if line.startswith("- "))
    if len(entries) != raw:
        raise ValueError(
            f"parsed {len(entries)} entries from {raw} listing lines; "
            "the parser is dropping entries rather than failing, which is the "
            "defect that lost six namespaced plugin skills"
        )
    return RequestResult(
        model=body.get("model"),
        entries=entries,
        rendered_chars=len(block),
    )


def request(
    cwd: str | Path | None = None,
    model: str = "haiku",
    dojo_skills_root: Path | None = None,
) -> RequestResult:
    """ """






    with tempfile.TemporaryDirectory() as tmp:
        _claude(["--model", model], cwd, env={"OTEL_LOG_RAW_API_BODIES": f"file:{tmp}"})
        files = sorted(glob.glob(f"{tmp}/*.request.json"))
        if not files:
            raise RuntimeError("probe B produced no request body")
        body = json.loads(Path(files[0]).read_text())
    result = parse_request(body)
    root = dojo_skills_root or (Path(__file__).resolve().parents[2] / "skills")
    return classify(result, root, Path(cwd or ".") / ".claude" / "skills")


def fingerprint(model: str = "haiku") -> dict:
    """ """








    try:
        version = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except FileNotFoundError:
        version = None
    return {
        "harness": "claude-code",
        "version": version,
        "model": model,
        "budget_fraction": SKILL_LISTING_BUDGET_FRACTION,
        "max_desc_chars": SKILL_LISTING_MAX_DESC_CHARS,
        "budget_unit": "characters",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--from-debug", help="parse a captured --debug-file instead of probing")
    parser.add_argument("--from-request", help="parse a captured request body instead of probing")
    args = parser.parse_args(argv)

    payload: dict = {}
    if args.from_debug or args.from_request:
        if args.from_debug:
            payload["debug"] = asdict(parse_debug(Path(args.from_debug).read_text()))
        if args.from_request:
            result = parse_request(json.loads(Path(args.from_request).read_text()))
            payload["request"] = asdict(result) | {
                "description_removed": result.description_removed,
                "ellipsis_truncated": result.ellipsis_truncated,
            }
    else:
        dbg = debug(args.cwd, args.model)
        req = request(args.cwd, args.model)
        payload["debug"] = asdict(dbg)
        payload["request"] = asdict(req) | {
            "description_removed": req.description_removed,
            "ellipsis_truncated": req.ellipsis_truncated,
        }

    if args.json:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        if dbg := payload.get("debug"):
            print(f"loaded            : {dbg['loaded']}")
            print(f"sent (the listing): {dbg['sent']}")
            print(f"demand chars      : {dbg['demand_chars']}")
            print(f"budget chars      : {dbg['budget_chars']}")
            if dbg["demand_chars"] and dbg["budget_chars"]:
                print(f"ratio             : {dbg['demand_chars'] / dbg['budget_chars']:.2f}x")
        if req := payload.get("request"):
            origins = collections.Counter(e["origin"] for e in req["entries"])
            print(f"origins           : {dict(origins)}")
            print(f"rendered chars    : {req['rendered_chars']}  <- never a cost")
            print(f"descriptions gone : {req['description_removed']}")
            print(f"ellipsis-truncated: {req['ellipsis_truncated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
