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
- MODEL / REASONING-EFFORT CAVEAT (asterisk): devin's `--model` takes a bare
  model id (e.g. "gpt-5.5") and exposes NO reasoning-effort selector, so the
  canonical "gpt-5.5-medium" maps to plain "gpt-5.5" and effort is whatever
  devin's default is — NOT independently pinnable. This is the one harness where
  the "medium" pin is not verifiable; it carries an asterisk in the report.
  (The dashed form "gpt-5-5-medium" is devin's TUI CONFIG representation, NOT a
  valid CLI value — passing it errors "Unknown model". A dashed id briefly
  appeared to work under an older devin build but no longer does; reverted.)
- Uses the user's existing devin login as-is (read-only).
- M4 OPEN MODELS (glm-*/deepseek-*/kimi-*) are NOT wired here: devin's `--model`
  is a closed, account-bound menu with no custom-provider/base-URL override, so
  the open canonicals fall through to the unsupported-model dict. (devin's menu
  DOES host some open models, e.g. glm-5.2 / kimi-k2.7 via devin's OWN serving —
  a different serving path from our first-party endpoints, so intentionally kept
  out of the M4 open panel.)
- devin prints no usage on stdout, but `--export <path>` writes a JSON
  conversation dump (to an ABSOLUTE temp path OUTSIDE workdir, so the workspace
  the checker inspects stays clean). Token accounting (see ``_parse_export``)
  has TWO bases depending on whether the model reports prompt caching:
    * cache REPORTED (total_cached_tokens > 0): a MEASURED count,
      tokens = total_prompt_tokens - total_cached_tokens + total_completion_tokens
      (fresh input+output, cache re-reads excluded — matches the other adapters).
    * cache NOT reported (total_cached_tokens 0/None — the account-default model's
      case): total_prompt_tokens is CUMULATIVE across steps (each step re-sends
      the whole context), which massively over-counts (e.g. ~185k on a multi-step
      task, 8-40x the caching harnesses). We instead report a CACHE-EQUIVALENT
      ESTIMATE = last model step's prompt_tokens + total_completion_tokens — i.e.
      what a caching provider WOULD bill (peak context + all generated tokens).
      This is an ESTIMATE, not a measured count.
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


def _empty_token_usage():
    return {
        "tokens_input_uncached": None,
        "tokens_cache_read": None,
        "tokens_cache_write": None,
        "tokens_output": None,
        "tokens_reasoning": None,
        "usage_raw": None,
        "token_basis": None,
    }

# canonical model name -> devin `--model` string.
# devin has no CLI reasoning-effort selector; the canonical medium pin collapses
# to plain "gpt-5.5" (the dashed "gpt-5-5-medium" is a TUI-config id, not a valid
# CLI value -> "Unknown model"). Effort is unpinned -> asterisk in the report.
MODELS = {
    "gpt-5.5-medium": "gpt-5.5",
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


def _err_tail(exc, limit=2000):
    """Last `limit` chars of a TimeoutExpired's captured output, decoding safely.

    On TimeoutExpired, `.stdout`/`.stderr` may be bytes (even under text=True),
    str, or None. Concatenating bytes with the ``""`` fallback raises TypeError,
    so decode each part first — the handler must always yield a clean tail.
    """
    def _dec(x):
        if x is None:
            return ""
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else x
    text = _dec(exc.stdout) + _dec(exc.stderr)
    return text if limit is None else text[-limit:]


def _parse_export_with_usage(path):
    """Parse devin's --export JSON into (tokens, turns, token_usage).

    Devin lacks a verified split surface. Preserve the legacy estimated scalar
    and leave normalized split lanes unknown per TOKEN_PARITY.md.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None, None, _empty_token_usage()

    fm = data.get("final_metrics") or {}
    prompt = fm.get("total_prompt_tokens")
    completion = fm.get("total_completion_tokens")
    cached = fm.get("total_cached_tokens")

    steps = data.get("steps") or []
    model_steps = [s for s in steps
                   if isinstance(s, dict) and isinstance(s.get("metrics"), dict)
                   and "prompt_tokens" in s["metrics"]]

    tokens = None
    if isinstance(prompt, (int, float)) and isinstance(completion, (int, float)):
        prompt, completion = int(prompt), int(completion)
        # Cache-equivalent estimate = last step's (peak) context + all output.
        last_prompt = (int(model_steps[-1]["metrics"]["prompt_tokens"])
                       if model_steps else prompt)
        if isinstance(cached, (int, float)) and cached > 0:
            # Cache REPORTED: measured fresh input+output, cache re-reads excluded.
            fresh = prompt - int(cached) + completion
            tokens = fresh if fresh >= completion else last_prompt + completion
        else:
            # Cache NOT reported: total_prompt_tokens is cumulative across steps
            # (over-counts); report the cache-equivalent ESTIMATE instead. See
            # the module docstring.
            tokens = last_prompt + completion

    token_usage = _empty_token_usage()
    if tokens is not None:
        token_usage["usage_raw"] = fm
        token_usage["token_basis"] = "estimated"

    turns = len(model_steps) or None
    return tokens, turns, token_usage



def _parse_export(stdout):
    """Backward-compatible parser returning legacy fields only."""
    tokens, turns, token_usage = _parse_export_with_usage(stdout)
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
            **_empty_token_usage(),
        }

    # Export goes to an absolute temp path OUTSIDE workdir so it never pollutes
    # the workspace the checker runs against.
    fd, export_path = tempfile.mkstemp(prefix="devin_export_", suffix=".json")
    os.close(fd)

    # devin's `--model` CLI values are broken on this account: `gpt-5.5` ->
    # "/upgrade to access this model", `gpt-5-5-medium` (the TUI id) -> "Unknown
    # model". Only the ACCOUNT DEFAULT (agent.model in ~/.config/devin/config.json,
    # currently "gpt-5-5-medium" = GPT-5.5 medium, set via the TUI) is accessible,
    # and it's used when --model is omitted. So we DON'T pass --model and run the
    # account-configured model. Effort/model is pinned by the user's devin config,
    # not our CLI -> keeps the reasoning-effort asterisk. MODELS still gates which
    # canonical names this adapter accepts.
    cmd = [
        "devin", "-p",
        "--permission-mode", "accept-edits",
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
            full_output = _err_tail(e, limit=None)
            return {
                "completed": False,
                "error": f"timeout after {timeout_s}s",
                "output_tail": full_output[-2000:],
                "full_output": full_output,
                "tokens": None,
                "turns": None,
                "cmd": cmd,
                **_empty_token_usage(),
            }

        combined = (proc.stdout or "") + (proc.stderr or "")
        try:
            tokens, turns, token_usage = _parse_export_with_usage(export_path)
        except Exception:  # noqa: BLE001 - never let usage parsing break a run
            tokens, turns, token_usage = None, None, _empty_token_usage()

        return {
            "completed": proc.returncode == 0,
            "error": None if proc.returncode == 0 else f"exit {proc.returncode}",
            "output_tail": combined[-2000:],
            # Optional (ADAPTER_SPEC v1): full untruncated stdout+stderr for the
            # runner's local transcript. LOCAL-ONLY; never published unscrubbed.
            "full_output": combined,
            "tokens": tokens,
            "turns": turns,
            "cmd": cmd,
            **token_usage,
        }
    finally:
        try:
            os.unlink(export_path)
        except OSError:
            pass
