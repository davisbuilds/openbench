#!/usr/bin/env python3
"""Inspect and update the CLI pins baked into the OpenBench Docker image.

This helper never pushes. ``--apply`` builds locally so it can verify the new
image before committing the Dockerfile pin change.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .paths import PACKAGE_DIR, SOURCE_ROOT

HERE = PACKAGE_DIR
REPO = SOURCE_ROOT
DOCKERFILE = os.path.join(HERE, "docker", "Dockerfile")
DEFAULT_IMAGE = "openbench-harness:latest"
VERSION_FILE = "/etc/openbench-cli-versions.json"
IMAGE_LABEL_PREFIX = "org.openbench.cli."


@dataclass(frozen=True)
class CliPin:
    key: str
    package: str | None
    arg: str
    harness: str
    cli: str


PINS = (
    CliPin("codex", "@openai/codex", "CODEX_VERSION", "codex", "codex"),
    CliPin("pi", "@earendil-works/pi-coding-agent", "PI_VERSION", "pi", "pi"),
    CliPin("claude", "@anthropic-ai/claude-code", "CLAUDE_VERSION", "claude", "claude"),
    CliPin("grok", "@xai-official/grok", "GROK_VERSION", "grokbuild", "grok"),
    CliPin("opencode", "opencode-ai", "OPENCODE_VERSION", "opencode", "opencode"),
    CliPin("cursor", None, "CURSOR_AGENT_VERSION", "cursor", "cursor-agent"),
)

PIN_BY_KEY = {pin.key: pin for pin in PINS}
PIN_BY_PACKAGE = {pin.package: pin for pin in PINS if pin.package}
PIN_BY_ARG = {pin.arg: pin for pin in PINS}


class CommandError(RuntimeError):
    pass


def run_cmd(cmd, *, cwd=REPO, check=True, text=True):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=text)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise CommandError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return proc


def read_dockerfile(path=DOCKERFILE):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write_dockerfile(text, path=DOCKERFILE):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def repo_relpath(path):
    abs_path = os.path.abspath(path)
    try:
        if os.path.commonpath([REPO, abs_path]) != REPO:
            return None
    except ValueError:
        return None
    return os.path.relpath(abs_path, REPO)


def ensure_git_clean(path):
    relpath = repo_relpath(path)
    if relpath is None:
        return
    proc = run_cmd(["git", "status", "--porcelain", "--", relpath])
    if (proc.stdout or "").strip():
        raise CommandError(
            f"refusing --apply with pre-existing changes in {relpath}; "
            "commit/stash them first so the automated commit contains only the pin bump"
        )


def dockerfile_pins(text):
    pins = {}
    for match in re.finditer(r"^ARG\s+([A-Z_]+)=(\S+)\s*$", text, re.MULTILINE):
        pin = PIN_BY_ARG.get(match.group(1))
        if pin:
            pins[pin.key] = match.group(2)
    return pins


def pinned_versions(path=DOCKERFILE):
    """Read the authoritative CLI versions from Dockerfile ARG pins."""
    return dockerfile_pins(read_dockerfile(path))


def parse_image_pin_labels(output):
    """Parse Docker inspect's JSON label object into ``{pin_key: version}``."""
    try:
        labels = json.loads(output or "")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(labels, dict):
        return {}
    versions = {}
    for pin in PINS:
        value = labels.get(IMAGE_LABEL_PREFIX + pin.key)
        if isinstance(value, str) and value.strip():
            versions[pin.key] = value.strip()
    return versions


def image_pin_mismatches(expected, actual, keys=None):
    """Return exact image-label mismatches against authoritative pin values."""
    mismatches = []
    for key in expected if keys is None else keys:
        pin = PIN_BY_KEY.get(key)
        wanted = expected.get(key)
        if pin is None or wanted is None:
            continue
        found = actual.get(key)
        if found != wanted:
            mismatches.append({
                "key": key,
                "harness": pin.harness,
                "cli": pin.cli,
                "expected": wanted,
                "actual": found or "missing label",
            })
    return mismatches


def reported_version(output):
    """Extract the first version token from a CLI's ``--version`` output."""
    match = re.search(
        r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+)+(?:-[A-Za-z0-9.-]+)?)",
        output or "",
    )
    return match.group(1) if match else None


def host_cli_version(pin, *, command_runner=run_cmd):
    """Return ``(parsed_version, raw_output)`` for one host CLI."""
    try:
        proc = command_runner([pin.cli, "--version"], check=False)
    except OSError as exc:
        return None, str(exc)
    raw = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return None, raw
    return reported_version(raw), raw


def rewrite_dockerfile_pins(text, updates):
    """Return Dockerfile text with selected ARG pin values replaced."""
    normalized = {resolve_pin_key(key): value for key, value in updates.items()}
    seen = set()

    def repl(match):
        arg = match.group(1)
        pin = PIN_BY_ARG.get(arg)
        if not pin or pin.key not in normalized:
            return match.group(0)
        seen.add(pin.key)
        return f"ARG {arg}={normalized[pin.key]}"

    new_text = re.sub(r"^ARG\s+([A-Z_]+)=(\S+)\s*$", repl, text, flags=re.MULTILINE)
    missing = sorted(set(normalized) - seen)
    if missing:
        raise ValueError(f"Dockerfile missing pin ARG(s): {', '.join(missing)}")
    return new_text


def resolve_pin_key(name):
    if name in PIN_BY_KEY:
        return name
    if name in PIN_BY_PACKAGE:
        return PIN_BY_PACKAGE[name].key
    for pin in PINS:
        if name == pin.arg or name == pin.harness or name == pin.cli:
            return pin.key
    raise ValueError(f"unknown CLI/package pin {name!r}")


def npm_latest(package):
    proc = run_cmd(["npm", "view", package, "version"], check=True)
    return (proc.stdout or "").strip().splitlines()[-1].strip()


def latest_versions():
    latest = {}
    for pin in PINS:
        if pin.package:
            latest[pin.key] = npm_latest(pin.package)
    return latest


def registry_latest(package, *, opener=None):
    """Return npm's ``latest`` dist-tag without invoking or installing npm."""
    opener = opener or urllib.request.urlopen
    encoded_package = urllib.parse.quote(package, safe="")
    request = urllib.request.Request(
        f"https://registry.npmjs.org/{encoded_package}",
        headers={"Accept": "application/vnd.npm.install-v1+json"},
    )
    with opener(request, timeout=10) as response:
        metadata = json.load(response)
    latest = metadata.get("dist-tags", {}).get("latest")
    if not isinstance(latest, str) or not latest.strip():
        raise ValueError("registry response has no latest dist-tag")
    return latest.strip()


def upstream_rows(current, *, latest_lookup=registry_latest):
    """Compare Dockerfile pins with npm, skipping failed registry lookups."""
    rows = []
    for pin in PINS:
        if not pin.package:
            rows.append({
                "key": pin.key,
                "package": "(manual)",
                "current": current.get(pin.key),
                "latest": "n/a",
                "up_to_date": None,
                "warning": None,
            })
            continue
        try:
            latest = latest_lookup(pin.package)
            warning = None
            up_to_date = current.get(pin.key) == latest
        except Exception as exc:  # noqa: BLE001 - one registry failure must not mask other packages
            latest = "unknown"
            warning = str(exc)
            up_to_date = None
            print(f"WARN: {pin.key}: npm registry lookup failed; skipped: {exc}", file=sys.stderr)
        rows.append({
            "key": pin.key,
            "package": pin.package,
            "current": current.get(pin.key),
            "latest": latest,
            "up_to_date": up_to_date,
            "warning": warning,
        })
    return rows


def check_rows(current):
    rows = []
    for pin in PINS:
        if not pin.package:
            rows.append({
                "key": pin.key,
                "package": "(installer)",
                "current": current.get(pin.key),
                "latest": "n/a",
                "up_to_date": None,
            })
            continue
        try:
            latest = npm_latest(pin.package)
            up_to_date = (current.get(pin.key) == latest)
        except Exception as exc:  # noqa: BLE001 - --check must exit 0 always
            latest = f"ERROR: {exc}"
            up_to_date = False
        rows.append({
            "key": pin.key,
            "package": pin.package,
            "current": current.get(pin.key),
            "latest": latest,
            "up_to_date": up_to_date,
        })
    return rows


def print_check_table(rows, *, pin_header="current", upstream=False):
    headers = ("key", "package", pin_header, "latest", "status")
    data = []
    for row in rows:
        if row.get("warning"):
            status = "skipped"
        elif row["up_to_date"] is None:
            status = "manual"
        elif row["up_to_date"]:
            status = "current" if upstream else "ok"
        else:
            status = "behind" if upstream else "update"
        data.append((row["key"], row["package"], row["current"] or "", row["latest"] or "", status))
    widths = [len(h) for h in headers]
    for item in data:
        widths = [max(width, len(str(value))) for width, value in zip(widths, item)]
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    print(fmt.format(*headers))
    print(fmt.format(*( "-" * width for width in widths)))
    for item in data:
        print(fmt.format(*item))


def parse_set(values):
    updates = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError("--set values must be pkg=version")
        name, version = value.split("=", 1)
        name = name.strip()
        version = version.strip()
        if not name or not version:
            raise ValueError("--set values must be pkg=version")
        updates[resolve_pin_key(name)] = version
    return updates


def docker_build(image):
    cmd = ["docker", "build", "-t", image, os.path.join("bench", "docker")]
    print("+ " + " ".join(cmd))
    run_cmd(cmd)


def docker_image_digest(image):
    proc = run_cmd(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    return (proc.stdout or "").strip()


def docker_cli_version(image, cli):
    proc = run_cmd(["docker", "run", "--rm", image, cli, "--version"])
    return (proc.stdout or "").strip()


def docker_version_file(image):
    proc = run_cmd(["docker", "run", "--rm", image, "cat", VERSION_FILE])
    return json.loads(proc.stdout)


def verify_image(image, expected):
    versions = docker_version_file(image)
    errors = []
    for pin in PINS:
        version = versions.get(pin.harness)
        if not isinstance(version, str) or not version.strip():
            errors.append(f"{pin.harness}: missing from {VERSION_FILE}")
        elif expected.get(pin.key) and expected[pin.key] not in version:
            errors.append(f"{pin.harness}: version file {version!r} does not contain pin {expected[pin.key]!r}")
        cli_version = docker_cli_version(image, pin.cli)
        if not cli_version:
            errors.append(f"{pin.cli} --version produced no output")
        elif version and cli_version != version:
            errors.append(f"{pin.cli}: --version {cli_version!r} != version file {version!r}")
    if errors:
        raise CommandError("image verification failed:\n" + "\n".join(errors))
    return versions


def git_commit_dockerfile(path=DOCKERFILE):
    relpath = repo_relpath(path)
    if relpath is None:
        raise CommandError(f"cannot commit Dockerfile outside repository: {path}")
    run_cmd(["git", "add", relpath])
    run_cmd(["git", "commit", "-m", "Bump OpenBench Docker CLI pins", "--", relpath])


def sync_host(dockerfile=DOCKERFILE, *, command_runner=run_cmd):
    """Install npm CLIs at Dockerfile pins; leave Cursor for manual install."""
    current = pinned_versions(dockerfile)
    missing = [pin.key for pin in PINS if pin.key not in current]
    if missing:
        raise CommandError("Dockerfile missing pin ARG(s): " + ", ".join(missing))

    for pin in PINS:
        expected = current[pin.key]
        before, before_raw = host_cli_version(pin, command_runner=command_runner)
        print(f"{pin.key}: before={before or before_raw or 'unavailable'} pin={expected}")
        if pin.package is None:
            status = "already matches" if before == expected else "manual sync required"
            print(f"{pin.key}: {status}; install cursor-agent {expected}, then run: "
                  f"{pin.cli} --version")
            print(f"{pin.key}: after={before or before_raw or 'unavailable'} pin={expected}")
            continue
        if before != expected:
            cmd = ["npm", "install", "-g", f"{pin.package}@{expected}"]
            print("+ " + " ".join(cmd))
            command_runner(cmd)
        after, after_raw = host_cli_version(pin, command_runner=command_runner)
        print(f"{pin.key}: after={after or after_raw or 'unavailable'} pin={expected}")
        if after != expected:
            raise CommandError(
                f"{pin.key}: host version {after or 'unavailable'} does not match pin {expected}"
            )


def refresh_baselines(image):
    print("# Rebuild/verify the standing Terminal-Bench frontier baseline on the refreshed Docker image:")
    print(
        "python3 bench/run_matrix.py --docker "
        "--harness pi,codex,opencode "
        "--model gpt-5.5-medium "
        "--task terminal-bench/count-call-stack,"
        "terminal-bench/feal-differential-cryptanalysis,"
        "terminal-bench/llm-inference-batching-scheduler,"
        "terminal-bench/schemelike-metacircular-eval "
        "--tasks-dir tasks-imported "
        "--trials 3 "
        "--out results/tb-frontier.jsonl "
        "--skip-gate"
    )
    print(f"# Uses bench/run.py's default Docker image tag; build it first as: docker build -t {image} bench/docker")


def apply(args):
    ensure_git_clean(args.dockerfile)
    text = read_dockerfile(args.dockerfile)
    current = dockerfile_pins(text)
    updates = parse_set(args.set)
    if not updates:
        updates = latest_versions()
    expected = dict(current)
    expected.update(updates)
    new_text = rewrite_dockerfile_pins(text, updates)
    wrote_update = new_text != text
    try:
        if wrote_update:
            write_dockerfile(new_text, args.dockerfile)
        docker_build(args.image)
        versions = verify_image(args.image, expected)
        digest = docker_image_digest(args.image)
    except Exception:
        if wrote_update:
            write_dockerfile(text, args.dockerfile)
        raise
    print(json.dumps({"image": args.image, "digest": digest, "versions": versions}, sort_keys=True))
    if wrote_update:
        git_commit_dockerfile(args.dockerfile)
    else:
        print("Dockerfile pins already up to date; no commit created")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dockerfile", default=DOCKERFILE, help=argparse.SUPPRESS)
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"Docker image tag (default: {DEFAULT_IMAGE})")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="print current vs npm latest pins; always exits 0")
    group.add_argument("--check-upstream", action="store_true",
                       help="query npm registry latest tags; exit 1 when a pin is behind")
    group.add_argument("--apply", action="store_true", help="rewrite pins, build/verify image, and commit Dockerfile")
    group.add_argument("--sync-host", action="store_true",
                       help="install host npm CLIs at Dockerfile pins; never build the image")
    group.add_argument("--refresh-baselines", action="store_true", help="print baseline rerun commands only")
    parser.add_argument("--json", action="store_true", help="emit JSON for --check or --check-upstream")
    parser.add_argument("--set", action="append", default=[], metavar="PKG=VERSION", help="override a pin during --apply")
    args = parser.parse_args(argv)

    if args.check:
        try:
            current = dockerfile_pins(read_dockerfile(args.dockerfile))
        except Exception as exc:  # noqa: BLE001 - --check is advisory and must exit 0
            print(f"WARN: could not read Dockerfile pins: {exc}", file=sys.stderr)
            current = {}
        rows = check_rows(current)
        if args.json:
            print(json.dumps(rows, sort_keys=True))
        else:
            print_check_table(rows)
        return 0
    if args.check_upstream:
        try:
            current = dockerfile_pins(read_dockerfile(args.dockerfile))
        except Exception as exc:  # noqa: BLE001 - keep the report useful when a path is temporarily unavailable
            print(f"WARN: could not read Dockerfile pins: {exc}", file=sys.stderr)
            current = {}
        rows = upstream_rows(current)
        if args.json:
            print(json.dumps(rows, sort_keys=True))
        else:
            print_check_table(rows, pin_header="pin", upstream=True)
        return 1 if any(row["up_to_date"] is False for row in rows) else 0
    if args.refresh_baselines:
        refresh_baselines(args.image)
        return 0
    try:
        if args.sync_host:
            sync_host(args.dockerfile)
        else:
            apply(args)
    except (CommandError, ValueError) as exc:
        print(f"bump_clis: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
