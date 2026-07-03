"""Adapter for the `devin` CLI (Cognition Devin, terminal mode).

Headless invocation:
    devin -p --permission-mode accept-edits --model gpt-5.5 \
        --export <tmp.json> -- <instruction>

Notes / quirks:
- `-p/--print` runs non-interactively (process prompt, exit).
- `--permission-mode accept-edits` auto-approves read-only tools AND workspace
  edits, so file changes happen unattended. It does NOT auto-approve arbitrary
  destructive actions (that would be "dangerous"), so it's the least-privileged
  mode that lets the agent edit files.
- In print mode `--respect-workspace-trust` defaults to false, so a fresh temp
  workdir does not trigger a trust prompt. No extra flag needed.
- The prompt is passed after `--` so a leading dash in an instruction can never
  be parsed as a flag.
- cwd=workdir; the agent edits files there.
- MODEL / REASONING EFFORT: devin DOES accept effort-pinned model ids, written
  dash-separated: `gpt-5-5-medium` (confirmed from the TUI-persisted value
  `agent.model` in ~/.config/devin/config.json, and verified live in `-p` mode).
  So the canonical "gpt-5.5-medium" maps to "gpt-5-5-medium" and the medium
  effort IS pinned. NOTE: the M3 dataset (2026-07-02) predates this finding and
  ran with the unpinned "gpt-5.5", so it keeps its asterisk; M3.5 onward is
  effort-pinned.
- Uses the user's existing devin login as-is (read-only).
- M4 OPEN MODELS (glm-*/deepseek-*/kimi-*) are NOT supported here: devin's
  `--model` is a closed, account-bound menu with no custom-provider/base-URL
  override, so open canonicals fall through to the unsupported-model dict.
- devin prints no usage on stdout, but `--export <path>` writes a JSON
  conversation dump (to an ABSOLUTE temp path OUTSIDE workdir, so the workspace
  the checker inspects stays clean). Token accounting (see ``_parse_export``):
    tokens = (total_prompt_tokens - total_cached_tokens) + total_completion_tokens
             (fresh input+output, cache re-reads excluded to match the other
             adapters' definition).
    turns  = number of steps that carry model metrics (model rounds).
  output_tail stays the human-readable stdout. Parsing is defensive: a missing
  or drifted export yields tokens=None/turns=None, never raising.
"""

import json
import os
import shutil
import subprocess
import tempfile

NAME = "devin"
_EXE = "devin"

# canonical model name -> devin `--model` string.
# devin accepts effort-pinned ids dash-separated (verified live + from the
# TUI-persisted agent.model in ~/.config/devin/config.json).
MODELS = {
    "gpt-5.5-medium": "gpt-5-5-medium",
}


def version():
    """Return the CLI version string (with binary path), or None on failure.

    Cheap `devin --version`; never raises (the runner calls this defensively).
    """
    try:
        proc = subprocess.run(
            [_EXE, "--version"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - version probing must never raise
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    path = shutil.which(_EXE)
    return f"{out} ({path})" if path else out


def _parse_export(path):
    """Parse devin's --export JSON into (tokens, turns).

    Returns (None, None) if the file is missing or the shape drifts.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None, None

    fm = data.get("final_metrics") or {}
    prompt = fm.get("total_prompt_tokens")
    completion = fm.get("total_completion_tokens")
    cached = fm.get("total_cached_tokens") or 0
    tokens = None
    if isinstance(prompt, (int, float)) and isinstance(completion, (int, float)):
        fresh = int(prompt) - int(cached) + int(completion)
        # Guard against a different cache accounting (cached not a subset of
        # prompt): never report a negative or absurdly small count.
        tokens = fresh if fresh >= int(completion) else int(prompt) + int(completion)

    steps = data.get("steps") or []
    turns = sum(1 for s in steps
                if isinstance(s, dict) and isinstance(s.get("metrics"), dict)
                and "prompt_tokens" in s["metrics"]) or None

    return tokens, turns


def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    if model not in MODELS:
        return {
            "completed": False,
            "error": f"unsupported-model: {model!r} (have {list(MODELS)})",
            "output_tail": "",
            "tokens": None,
            "turns": None,
            "cmd": None,
        }

    # Export goes to an absolute temp path OUTSIDE workdir so it never pollutes
    # the workspace the checker runs against.
    fd, export_path = tempfile.mkstemp(prefix="devin_export_", suffix=".json")
    os.close(fd)

    cmd = [
        "devin", "-p",
        "--permission-mode", "accept-edits",
        "--model", MODELS[model],
        "--export", export_path,
        "--", instruction,
    ]

    try:
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            tail = ((e.stdout or "") + (e.stderr or ""))[-2000:]
            return {
                "completed": False,
                "error": f"timeout after {timeout_s}s",
                "output_tail": tail,
                "tokens": None,
                "turns": None,
                "cmd": cmd,
            }

        combined = (proc.stdout or "") + (proc.stderr or "")
        try:
            tokens, turns = _parse_export(export_path)
        except Exception:  # noqa: BLE001 - never let usage parsing break a run
            tokens, turns = None, None

        return {
            "completed": proc.returncode == 0,
            "error": None if proc.returncode == 0 else f"exit {proc.returncode}",
            "output_tail": combined[-2000:],
            "tokens": tokens,
            "turns": turns,
            "cmd": cmd,
        }
    finally:
        try:
            os.unlink(export_path)
        except OSError:
            pass
