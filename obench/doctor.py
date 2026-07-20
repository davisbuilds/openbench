#!/usr/bin/env python3
"""Preflight doctor for the agent-harness benchmark.

Run this BEFORE a matrix run to catch missing CLIs / auth / model pins without
spending any tokens (no live model calls). Per requested harness it verifies:

  1. CLI     - the harness binary is installed (which + --version captured)
  2. AUTH    - the adapter-specific credential is present, mirroring exactly
               what each bench/adapters/<name>.py expects at run time
  3. MODEL   - the adapter module imports and its MODELS maps the requested
               canonical model name

Docker daemon/image availability is informational. A successfully inspected
image whose labels drift from the Dockerfile pins fails the preflight.

    python3 bench/doctor.py [--harness codex,pi,...] [--model gpt-5.5-medium]

Exit status is nonzero if any requested harness fails any of CLI/AUTH/MODEL.

Auth expectations are mirrored from the adapters (read them, don't invent):
  codex     ~/.codex/auth.json exists (adapter uses ~/.codex login as-is)
  codex_v1/codex_v2 same auth as codex; adapters compose temp CODEX_HOME
  pi        ~/.pi/agent/auth.json exists AND has an "openai-codex" or
            "anthropic" entry (adapter's isolated-HOME route reads this file)
  opencode  `opencode auth list` shows an OpenAI oauth credential (adapter
            strips OPENAI_API_KEY to force the subscription OAuth route)
  cursor    `cursor-agent status` exits 0 (existing Cursor login)
  claude    no ~/.claude mount; API-key routes require provider env keys
  grokbuild no ~/.grok mount; open routes need vendor keys, while gpt-5.6
            uses host-side CLIProxyAPI subscription OAuth
  devin     ~/.config/devin exists (existing devin login)

Python3 stdlib only.
"""

import argparse
import importlib.util
import json
import os
import re
import shlex
import http.client
import subprocess
from urllib.parse import urlsplit

from bump_clis import (image_pin_mismatches, parse_image_pin_labels,
                       pinned_versions, reported_version, resolve_pin_key)

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTERS_DIR = os.path.join(HERE, "adapters")
DEFAULT_MODEL = "gpt-5.5-medium"
DEFAULT_IMAGE = "openbench-harness:latest"

CHECKS = ("CLI", "VERSION", "AUTH", "MODEL")

# M4 open canonical model -> the env key its provider needs. When --model is one
# of these, the AUTH check becomes "is this key exported?" instead of the
# harness's own subscription-login check. Mirrors the adapters' OPEN_MODELS.
OPEN_MODEL_ENV = {
    "glm-5.2": "ZAI_API_KEY",
    "glm-4.7-flash": "ZAI_API_KEY",
    "deepseek-v4-flash": "DEEPSEEK_API_KEY",
    "kimi-k2.7-code": "MOONSHOT_API_KEY",
}
FRONTIER_MODEL_ENV = {
    "claude-opus-4-8": "ANTHROPIC_API_KEY",
}
KEYS_ENV = "~/.openbench/keys.env"


# --------------------------------------------------------------------------- #
# Probes: every side effect goes through this object so tests can mock it all
# and never touch the real CLIs, filesystem, or network.
# --------------------------------------------------------------------------- #
class Probes:
    """Real-world probes: subprocess, filesystem, adapter import."""

    def which(self, cli):
        """Absolute path to ``cli`` on PATH, or None."""
        from shutil import which
        return which(cli)

    def run(self, argv, timeout=15):
        """Run ``argv`` headlessly; return (exit_code|None, combined_output).

        exit_code is None if the command is missing or times out.
        """
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=timeout, stdin=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None, ""
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def exists(self, path):
        """True if ``path`` (file or dir) exists, expanding ``~``."""
        return os.path.exists(os.path.expanduser(path))

    def http_get(self, url, headers=None, timeout=2.0):
        """Return (status, body) for a small readiness GET, or (None, '')."""
        parsed = urlsplit(url)
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        try:
            conn = cls(parsed.hostname, parsed.port, timeout=timeout)
            conn.request("GET", parsed.path or "/", headers=headers or {})
            response = conn.getresponse()
            body = response.read(1024 * 1024).decode("utf-8", "replace")
            conn.close()
            return response.status, body
        except (OSError, http.client.HTTPException, ValueError):
            return None, ""

    def getenv(self, name):
        """Return the environment variable ``name`` (or None)."""
        return os.environ.get(name)

    def read_json(self, path):
        """Parse JSON at ``path`` (expanding ``~``); None if missing/invalid."""
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def read_text(self, path):
        """Read text at ``path`` (expanding ``~``); None if missing/unreadable."""
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def import_adapter(self, name):
        """Import ``bench/adapters/<name>.py`` and return the module."""
        path = os.path.join(ADAPTERS_DIR, f"{name}.py")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"adapter not found: {path}")
        spec = importlib.util.spec_from_file_location(f"doctor_adapter_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


# --------------------------------------------------------------------------- #
# Auth checks - one per harness, mirroring the adapter's own expectation.
# Each returns (ok: bool, detail: str).
# --------------------------------------------------------------------------- #
def _auth_codex(p):
    path = "~/.codex/auth.json"
    if p.exists(path):
        return True, os.path.expanduser(path)
    return False, f"missing {os.path.expanduser(path)}"


def _auth_pi_provider(p, provider):
    path = "~/.pi/agent/auth.json"
    if not p.exists(path):
        return False, f"missing {os.path.expanduser(path)}"
    data = p.read_json(path)
    if not isinstance(data, dict):
        return False, f"unreadable JSON at {os.path.expanduser(path)}"
    if provider in data:
        return True, f"entry: {provider}"
    return False, f"no {provider} entry in ~/.pi/agent/auth.json"


def _auth_pi(p):
    return _auth_pi_provider(p, "openai-codex")


def _auth_opencode(p):
    return _auth_opencode_provider(p, "openai")


def _auth_opencode_provider(p, provider):
    code, out = p.run(["opencode", "auth", "list"])
    if code is None:
        return False, "`opencode auth list` did not run"
    if code != 0:
        return False, f"`opencode auth list` exit {code}"
    # Subscription credentials print as lines mentioning provider + oauth; API
    # key env lines have no "oauth" and should not pass subscription checks.
    for line in out.splitlines():
        low = line.lower()
        if provider.lower() in low and "oauth" in low:
            return True, f"{provider} oauth credential present"
    return False, f"no {provider} oauth credential in `opencode auth list`"


def _auth_cursor(p):
    code, out = p.run(["cursor-agent", "status"])
    if code == 0:
        first = out.strip().splitlines()[0] if out.strip() else "logged in"
        return True, first
    return False, f"`cursor-agent status` exit {code}"


def _auth_devin(p):
    path = "~/.config/devin"
    if p.exists(path):
        return True, os.path.expanduser(path)
    return False, f"missing {os.path.expanduser(path)}"


# harness name -> {cli binary, auth checker}. The adapter module name equals the
# harness name (cursor's binary is cursor-agent but its adapter is cursor.py).
HARNESSES = {
    "codex":    {"cli": "codex",        "auth": _auth_codex},
    "codex_v1": {"cli": "codex",        "auth": _auth_codex},
    "codex_v2": {"cli": "codex",        "auth": _auth_codex},
    "pi":       {"cli": "pi",           "auth": _auth_pi},
    "opencode": {"cli": "opencode",     "auth": _auth_opencode},
    "cursor":   {"cli": "cursor-agent", "auth": _auth_cursor},
    "claude":   {"cli": "claude",       "auth": lambda p: (True, "API-key routes checked per model")},
    "grokbuild": {"cli": "grok",         "auth": lambda p: (True, "BYOK routes checked per model")},
    "devin":    {"cli": "devin",        "auth": _auth_devin},
}
# Default doctor preflight keeps the historical matrix harnesses for the default
# gpt-5.5-medium model; claude/grokbuild are opt-in because they support
# API-key/open-model routes, not the default ChatGPT subscription model.
ALL_HARNESSES = [h for h in HARNESSES if h not in {"claude", "grokbuild"}]


# --------------------------------------------------------------------------- #
# Individual check functions -> (ok, detail)
# --------------------------------------------------------------------------- #
def check_cli(p, cli):
    path = p.which(cli)
    if not path:
        return False, f"{cli} not found on PATH"
    _, out = p.run([cli, "--version"])
    ver = out.strip().splitlines()[0] if out.strip() else ""
    return True, f"{path} ({ver})" if ver else path


def check_version(p, harness, cli, pins):
    """Compare a host CLI's reported version with its Dockerfile pin."""
    base = "codex" if harness.startswith("codex_") else harness
    try:
        key = resolve_pin_key(base)
    except ValueError:
        return None, "no Dockerfile pin (n/a)"
    expected = pins.get(key)
    if expected is None:
        return False, f"host=unavailable pin=missing ({key}) [drift]"
    code, out = p.run([cli, "--version"])
    actual = reported_version(out) if code == 0 else None
    ok = actual == expected
    label = "ok" if ok else "drift"
    return ok, f"host={actual or 'unavailable'} pin={expected} [{label}]"


def check_model(p, harness, model):
    try:
        mod = p.import_adapter(harness)
    except Exception as exc:  # noqa: BLE001 - report any import failure as FAIL
        return False, f"adapter import failed: {exc}"
    models = getattr(mod, "MODELS", None)
    if not isinstance(models, dict):
        return False, "adapter exposes no MODELS dict"
    if model in models:
        return True, f"{model} -> {models[model]}"
    open_models = getattr(mod, "OPEN_MODELS", None)
    if isinstance(open_models, dict) and model in open_models:
        return True, f"{model} -> {open_models[model]['model_id']} (open)"
    known = list(models) + (list(open_models) if isinstance(open_models, dict) else [])
    return False, f"{model} not in MODELS/OPEN_MODELS {known}"


def _keys_env_has(p, env_key):
    text = p.read_text(KEYS_ENV)
    if text is None:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        try:
            parts = shlex.split(line, comments=True, posix=True)
            first = parts[1] if parts and parts[0] == "export" and len(parts) > 1 else parts[0]
            key, val = first.split("=", 1)
        except (ValueError, IndexError):
            key, val = line.split("=", 1)[0].strip(), line.split("=", 1)[1].strip()
        if key == env_key and val.strip():
            return True
    return False


def check_open_key(p, env_key, *, keys_env_ok=False):
    """AUTH check for API-key routes: env key exported, or keys.env if allowed."""
    if p.getenv(env_key):
        return True, f"{env_key} present"
    if keys_env_ok and _keys_env_has(p, env_key):
        return True, f"{env_key} present in {os.path.expanduser(KEYS_ENV)}"
    if keys_env_ok:
        return False, f"SETUP-NEEDED: export {env_key} or add it to {os.path.expanduser(KEYS_ENV)}"
    return False, f"SETUP-NEEDED: export {env_key}"


def check_subbridge(p):
    """Verify CLIProxyAPI is installed and its configured ingress is reachable."""
    configured_base = p.getenv("CLIPROXYAPI_BASE_URL")
    if not configured_base and not p.which("cliproxyapi"):
        return False, "SETUP-NEEDED: brew install cliproxyapi"
    base = configured_base or "http://127.0.0.1:8317/v1"
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "SETUP-NEEDED: CLIPROXYAPI_BASE_URL must be an absolute HTTP(S) URL"
    local_hosts = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
    if parsed.scheme == "http" and parsed.hostname not in local_hosts:
        return False, "SETUP-NEEDED: remote CLIPROXYAPI_BASE_URL must use HTTPS"
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False, "SETUP-NEEDED: CLIPROXYAPI_BASE_URL has an invalid port"
    models_url = base.rstrip("/") + "/models"
    ingress_key = p.getenv("CLIPROXYAPI_API_KEY") or "openbench-local-ingress"
    status, body = p.http_get(models_url, {"Authorization": f"Bearer {ingress_key}"})
    try:
        payload = json.loads(body)
        model_ids = {item.get("id") for item in payload.get("data", []) if isinstance(item, dict)}
    except (ValueError, AttributeError):
        model_ids = set()
    if status != 200 or "gpt-5.6" not in model_ids:
        return False, f"SETUP-NEEDED: CLIProxyAPI gpt-5.6 subscription route unavailable at {parsed.hostname}:{port}"
    return True, f"CLIProxyAPI gpt-5.6 route ready at {parsed.hostname}:{port}"


def _auth_cursor_container(p):
    path = "~/.openbench/cursor-container-auth/.config/cursor/auth.json"
    if p.getenv("CURSOR_API_KEY"):
        return True, "CURSOR_API_KEY present"
    if p.exists(path):
        return True, os.path.expanduser(path)
    return False, ("SETUP-NEEDED: run bench/cursor_container_login.sh "
                   f"or export CURSOR_API_KEY (missing {os.path.expanduser(path)})")


def _auth_frontier(p, harness, model):
    env_key = FRONTIER_MODEL_ENV[model]
    if harness == "pi":
        return _auth_pi_provider(p, "anthropic")
    if harness == "opencode":
        return _auth_opencode_provider(p, "anthropic")
    if harness == "cursor":
        if p.getenv("BENCH_IN_CONTAINER"):
            return _auth_cursor_container(p)
        return _auth_cursor(p)
    if harness in {"codex", "codex_v1", "codex_v2"}:
        return check_open_key(p, env_key, keys_env_ok=True)
    if harness == "claude":
        return check_open_key(p, env_key)
    return HARNESSES[harness]["auth"](p)


def check_docker(p):
    """Informational Docker daemon probe -> (ok|None, detail)."""
    if not p.which("docker"):
        return None, "docker not on PATH (informational)"
    code, out = p.run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if code == 0 and out.strip():
        return True, f"daemon up (server {out.strip().splitlines()[0]})"
    return False, "docker installed but daemon not responding"


def check_image_versions(p, harnesses, pins, image=DEFAULT_IMAGE):
    """Compare requested harness pins with one Docker image-label inspect."""
    code, out = p.run([
        "docker", "inspect", "--format", "{{json .Config.Labels}}", image,
    ])
    if code != 0:
        return None, (f"{image} unavailable; build with: "
                      f"docker build -t {image} bench/docker")
    labels = parse_image_pin_labels(out)
    keys = []
    for harness in harnesses:
        base = "codex" if harness.startswith("codex_") else harness
        try:
            key = resolve_pin_key(base)
        except ValueError:
            continue
        if key not in keys:
            keys.append(key)
    mismatches = image_pin_mismatches(pins, labels, keys)
    if mismatches:
        detail = "; ".join(
            f"{item['harness']}: image={item['actual']} pin={item['expected']}"
            for item in mismatches)
        return False, "drift: " + detail
    return True, f"{image} matches Dockerfile pins"


# --------------------------------------------------------------------------- #
# Evaluation + rendering
# --------------------------------------------------------------------------- #
def evaluate(harnesses, model, probes, pins=None):
    """Return ``(rows, ok)`` for the requested harnesses.

    ``rows`` is a list of dicts ``{harness, check, ok, detail}`` covering the
    CLI/AUTH/MODEL checks. ``ok`` (the second return value) is True iff every
    such check passed. Unknown harness names produce a single failing row.
    """
    rows = []
    all_ok = True
    pins = pinned_versions() if pins is None else pins
    for name in harnesses:
        spec = HARNESSES.get(name)
        if spec is None:
            rows.append({"harness": name, "check": "KNOWN", "ok": False,
                         "detail": f"unknown harness (have {ALL_HARNESSES})"})
            all_ok = False
            continue

        cli_ok, cli_detail = check_cli(probes, spec["cli"])
        version_ok, version_detail = check_version(probes, name, spec["cli"], pins)
        if name == "grokbuild" and model == "gpt-5.6":
            auth_ok, auth_detail = check_subbridge(probes)
        elif model in FRONTIER_MODEL_ENV:
            auth_ok, auth_detail = _auth_frontier(probes, name, model)
        elif model in OPEN_MODEL_ENV:
            # Open model: AUTH = provider env key present (harness login is moot).
            keys_env_ok = name in {"codex", "codex_v1", "codex_v2"}
            auth_ok, auth_detail = check_open_key(
                probes, OPEN_MODEL_ENV[model], keys_env_ok=keys_env_ok)
        else:
            auth_ok, auth_detail = spec["auth"](probes)
        model_ok, model_detail = check_model(probes, name, model)

        for check, ok, detail in (
            ("CLI", cli_ok, cli_detail),
            ("VERSION", version_ok, version_detail),
            ("AUTH", auth_ok, auth_detail),
            ("MODEL", model_ok, model_detail),
        ):
            rows.append({"harness": name, "check": check, "ok": ok,
                         "detail": detail})
            if ok is False:
                all_ok = False
    return rows, all_ok


def _status(ok, check=None):
    if ok is None:
        return "INFO"
    if not ok and check == "VERSION":
        return "DRIFT"
    return "OK" if ok else "FAIL"


def format_report(rows, harnesses, docker_row, image_row=None):
    """Render the status matrix + details + Docker/image status lines."""
    lines = []

    # Status matrix: one row per harness, one column per check.
    by_harness = {}
    for row in rows:
        by_harness.setdefault(row["harness"], {})[row["check"]] = row["ok"]

    headers = ["harness"] + list(CHECKS)
    table = []
    for name in harnesses:
        cells = [name]
        checks = by_harness.get(name, {})
        if "KNOWN" in checks:  # unknown harness -> collapse across columns
            cells += ["FAIL"] * len(CHECKS)
        else:
            cells += [_status(checks.get(c), c) for c in CHECKS]
        table.append(cells)

    widths = [len(h) for h in headers]
    for cells in table:
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))

    def fmt(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    lines.append(fmt(headers))
    lines.append(fmt(["-" * w for w in widths]))
    lines.extend(fmt(cells) for cells in table)

    # Details block (every check, so passes are auditable too).
    lines.append("")
    lines.append("Details:")
    for row in rows:
        lines.append(f"  [{_status(row['ok'], row['check']):>5}] {row['harness']:<9} "
                     f"{row['check']:<6} {row['detail']}")

    # Docker (informational).
    ok, detail = docker_row
    lines.append("")
    lines.append(f"Docker (informational): [{_status(ok)}] {detail}")
    if image_row is not None:
        image_ok, image_detail = image_row
        image_status = "DRIFT" if image_ok is False else _status(image_ok)
        lines.append(f"Image pins: [{image_status}] {image_detail}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark preflight doctor.")
    parser.add_argument("--harness", default=",".join(ALL_HARNESSES),
                        help="comma-separated harness names to check "
                             f"(default: all {ALL_HARNESSES})")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"canonical model to resolve (default: {DEFAULT_MODEL})")
    parser.add_argument("--docker-image", default=DEFAULT_IMAGE,
                        help=f"image to compare with pins (default: {DEFAULT_IMAGE})")
    args = parser.parse_args(argv)

    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    probes = Probes()

    pins = pinned_versions()
    rows, ok = evaluate(harnesses, args.model, probes, pins=pins)
    docker_row = check_docker(probes)
    image_row = check_image_versions(probes, harnesses, pins, args.docker_image)
    ok = ok and image_row[0] is not False
    print(format_report(rows, harnesses, docker_row, image_row))
    print()
    print(f"Preflight: {'PASS' if ok else 'FAIL'} "
          f"({len(harnesses)} harness(es), model={args.model})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
