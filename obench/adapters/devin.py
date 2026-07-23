"""Adapter for the `devin` CLI (Cognition Devin, terminal mode).

Headless invocation:
    devin -p --permission-mode accept-edits --model gpt-5-6-sol-medium \
        --export <tmp.json> -- <instruction>

Notes / quirks:
- `-p/--print` runs non-interactively (process prompt, exit).
- `--permission-mode dangerous` auto-approves all tools, including shell
  execution. Required for policy parity: every other harness auto-runs
  commands (tests, builds); accept-edits blocked devin's runtime verification
  ("Runtime verification was blocked by the CLI's non-interactive permission
  mode"), which is fatal on execution-dependent tasks (all of Terminal-Bench).
  Cells run in disposable temp workdirs with an isolated HOME, the same
  blast-radius posture as the other harnesses.
- In print mode `--respect-workspace-trust` defaults to false, so a fresh temp
  workdir does not trigger a trust prompt. No extra flag needed.
- The prompt is passed after `--` so a leading dash in an instruction can never
  be parsed as a flag.
- cwd=workdir; the agent edits files there.
- MODEL SELECTION (verified Jul 2026, devin v3000.2.17 on a Max plan):
  `--model` accepts dashed menu UIDs (e.g. "gpt-5-6-sol-medium", "glm-5-2");
  live probes confirm the flag is honored and per-step metrics report the
  selected model. Effort is part of the UID where devin exposes it
  ("grok-4-5-{low,medium,high}", "gpt-5-6-sol-medium"), so those canonicals
  ARE effort-pinned; UIDs without an effort suffix (e.g. "glm-5-2") run
  devin's default effort. Older builds behaved differently (bare ids only,
  dashed ids rejected, account-default fallback) — that lane is preserved
  solely for the legacy "gpt-5.5-medium" canonical, which still omits
  `--model` and runs the account-configured default.
- MENU COVERAGE (surveyed Jul 2026): deepseek does NOT appear in devin's model
  menu at all, and kimi is "kimi-k2-7" (k2.7), not k3 — so devin is EXCLUDED
  from the deepseek and kimi-k3 comparisons rather than substituted.
- SERVING-PATH CAVEAT: ALL inference happens behind Devin's cloud boundary.
  For open-weights models (e.g. glm-5.2) devin serves the model on its OWN
  infrastructure, a different serving path from the first-party endpoints the
  other harnesses use — provider-side behavior (quantization, sampling,
  context policy) is not independently verifiable. ``usage_raw`` carries a
  ``serving_path: "devin-cloud"`` marker so downstream reports can disclose
  this.
- Uses the user's existing devin login via a COPY of ~/.devin staged into a
  throwaway HOME (see ``_isolated_home``): the login works, but the user's
  global agent config — ~/.agents/skills, personal workflow instructions —
  cannot leak into cells (plan-approval stops and review-subagent hangs
  contaminated the 2026-07-20 devin arm this way).
- COUNTING PROXY UNSUPPORTED: the terminal CLI exposes no model-provider base
  URL or custom-provider mechanism. It authenticates to Cognition and receives
  a service-selected inference endpoint; model inference and usage accounting
  happen behind Devin's cloud boundary. `--config`, `DEVIN_MODEL`, and the
  config's generic network `proxy` setting do not redirect the model endpoint
  to an OpenAI/Anthropic-compatible URL. An HTTP(S) forward proxy would only
  see an encrypted CONNECT tunnel and cannot provide the response usage needed
  by `obench/proxy.py`, so `--proxy` deliberately leaves this lane unwired.
  Token numbers below are therefore SELF-REPORTED by devin's export, never
  proxy-measured.
- devin prints no usage on stdout, but `--export <path>` writes a JSON
  conversation dump (to an ABSOLUTE temp path OUTSIDE workdir, so the workspace
  the checker inspects stays clean). Token accounting (see ``_parse_export``)
  has TWO bases depending on whether the model reports prompt caching:
    * cache REPORTED (total_cached_tokens > 0): a MEASURED count,
      tokens = total_prompt_tokens - total_cached_tokens + total_completion_tokens
      (fresh input+output, cache re-reads excluded — matches the other adapters).
      Verified live for glm-5-2 (fresh = 16,358 on the probe task).
    * cache NOT reported (total_cached_tokens 0/None): total_prompt_tokens is
      CUMULATIVE across steps (each step re-sends the whole context), which
      massively over-counts (e.g. ~185k on a multi-step task, 8-40x the caching
      harnesses). We instead report a CACHE-EQUIVALENT ESTIMATE = last model
      step's prompt_tokens + total_completion_tokens — i.e. what a caching
      provider WOULD bill (peak context + all generated tokens). This is an
      ESTIMATE, not a measured count.
    turns  = number of steps that carry model metrics (model rounds).
  Either way ``token_basis`` stays "estimated": even the cache-reported count
  is devin's own accounting with no independent (proxy) verification.
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

# canonical model name -> devin `--model` UID (dashed menu ids, verified live
# on devin v3000.2.17 / Max plan). "grok-4-5-medium" matches the medium effort
# pin used by cursor in the grok-4.5 matrix; "glm-5-2" has no effort suffix in
# devin's menu (default effort). None -> omit --model and run the
# account-configured default (legacy gpt-5.5 lane; see module docstring).
MODELS = {
    "gpt-5.5-medium": None,
    "gpt-5.6-sol": "gpt-5-6-sol-medium",
    "grok-4.5": "grok-4-5-medium",
    "glm-5.2": "glm-5-2",
    "inkling": "inkling",
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
        # serving_path marker: all devin inference (including open-weights
        # models) runs behind Cognition's cloud, not first-party endpoints.
        token_usage["usage_raw"] = dict(fm, serving_path="devin-cloud")
        token_usage["token_basis"] = "estimated"

    turns = len(model_steps) or None
    return tokens, turns, token_usage



def _parse_export(stdout):
    """Backward-compatible parser returning legacy fields only."""
    tokens, turns, token_usage = _parse_export_with_usage(stdout)
    return tokens, turns

def _isolated_home(tmp_root=None):
    """Create a throwaway HOME with only devin's auth/config staged in.

    Devin's CLI discovers the invoking user's global agent config — shared
    skills in ~/.agents/skills (plan-loop, simplify, review subagents) and
    personal workflow instructions. Those contaminated benchmark cells: the
    agent solved tasks in under a minute, then followed the user's closeout
    ritual (plan approval, simplify, 3 review subagents) which dead-ends in
    print mode and burned the full wall cap. A fresh HOME containing just
    ~/.devin keeps the login while shutting out every user-level behavior
    source. The copy is discarded after the run and never written back.
    """
    home = tempfile.mkdtemp(prefix="devin_home_", dir=tmp_root)
    real = os.path.expanduser("~")
    # Auth is ~/.local/share/devin/credentials.toml (XDG data), config is
    # ~/.config/devin; ~/.devin covered for older builds. ~/.agents and
    # AGENTS.md are deliberately NOT staged — that's the whole point.
    for rel in (".devin", os.path.join(".config", "devin"),
                os.path.join(".local", "share", "devin")):
        src = os.path.join(real, rel)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(home, rel), symlinks=True)
    return home


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

    # Pass --model explicitly with the dashed menu UID (verified working on
    # devin v3000.2.17 / Max plan). The legacy gpt-5.5 canonical maps to None:
    # omit --model and run the account-configured default (older behavior).
    model_uid = MODELS[model]
    cmd = [
        "devin", "-p",
        "--permission-mode", "dangerous",
        *(["--model", model_uid] if model_uid else []),
        "--export", export_path,
        "--", instruction,
    ]

    iso_home = _isolated_home()
    env = {**os.environ, "HOME": iso_home}
    # Drop env-level behavior overrides so cells run devin's defaults.
    for var in ("DEVIN_PERMISSION_MODE", "DEVIN_MODEL"):
        env.pop(var, None)

    try:
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
                env=env,
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
        shutil.rmtree(iso_home, ignore_errors=True)
