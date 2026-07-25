#!/usr/bin/env python3
"""Failure taxonomy shared by the runner, reports, and backfill tool.

The runner classifies rows at write time using the adapter's full untruncated
output when adapters provide ``full_output``. Backfill can only use fields saved
in old JSONL rows (usually ``output_tail``), so old-file detection is inherently
weaker for markers that were truncated away.

Rows that ride the wall-time cap normally classify as ``timeout``. A cap-riding
row with no evidence of agent work is treated as harness/provider infra instead:
no tokens, no turns, and effectively empty saved output/text after generic
headers and timeout boilerplate are removed. Genuine timeouts still show work via
tokens, turns, or meaningful transcript/error text and remain ``timeout``.

Zero-work classifier gate (P0 hardening)
-----------------------------------------
A completed cell with zero output tokens AND at most 1 turn AND no meaningful
adapter-output text is infrastructure, never a capability "wrong answer."
The canonical pattern is a gpt-5.6-sol cell that completes in 2--9s wall time
with 0 output tokens and exactly 1 turn — the harness started, the model
returned nothing useful (connection dropped, auth rejected early, or the
adapter's minimal turn was a probe that got no response), and the cell
finished without the adapter crashing visibly.

This is symmetric with the crash-marker / adapter-traceback gate: when no
model output was produced, the failure belongs to the harness or provider, not
the model's ability to solve the task.
"""

import re

FAILURE_CLASSES = ("solved", "wrong_answer", "timeout", "rate_limited", "infra", "stalled")
EXCLUDED_FROM_SOLVE_RATE = ("rate_limited", "infra", "stalled")
NEAR_ZERO_TOKEN_LIMIT = 100
_TOKEN_FIELDS = (
    "tokens", "tokens_fresh", "tokens_input_uncached", "tokens_cache_read",
    "tokens_cache_write", "tokens_output", "tokens_reasoning",
    "tokens_proxy_input_uncached", "tokens_proxy_cache_read",
    "tokens_proxy_cache_write", "tokens_proxy_output", "tokens_proxy_reasoning",
)

# Rate-limit classification excludes rows from solve-rate denominators, so the
# detector intentionally requires provider/API-error context rather than any
# domain text that happens to mention e.g. HTTP 429 or a rate limiter task.
_RATE_LIMIT_RES = [
    re.compile(r"\b(?:APIError|API error|HTTPError|HTTP error|provider|vendor)\b[^\n]{0,200}\b(?:429|rate[_ -]?limit|TPD|quota|too many requests)", re.IGNORECASE),
    re.compile(r"\b(?:HTTP|status(?: code)?|response)\s*[:=]?\s*429\b[^\n]{0,200}\b(?:rate[_ -]?limit|TPD|quota|too many requests)", re.IGNORECASE),
    re.compile(r"\b(?:429|rate[_ -]?limit|TPD|quota|too many requests)\b[^\n]{0,200}\b(?:APIError|API error|HTTPError|HTTP error|provider|vendor|status(?: code)?|response)\b", re.IGNORECASE),
    # Moonshot/Kimi daily-token exhaustion uses this distinctive provider text.
    re.compile(r"\bTPD\b[^\n]{0,160}\b(?:current|limit|rate)", re.IGNORECASE),
]

_INFRA_RE = re.compile(
    r"("
    r"docker (?:daemon not reachable|unavailable|desktop)|"
    r"DockerUnavailable|"
    r"container produced no result sentinel|"
    r"no result sentinel|"
    r"SETUP-NEEDED|"
    r"missing [^\n]*(?:auth|credential|api[_ -]?key)|"
    r"No API key for provider|"
    r"not logged in|login required|please log in|"
    r"No such image:\s*[^\s]+|"
    r"image ['\"]?[^'\"\n]+['\"]? not found|"
    r"Cannot connect to the Docker daemon|"
    r"proxy_upstream_failed|"
    # Provider auth rejections in vendor/liteLLM phrasing (aider et al.):
    r"authentication_error|AuthenticationError|"
    r"api key[^\n]{0,40}\binvalid|invalid[^\n]{0,20}api key|incorrect api key"
    r")",
    re.IGNORECASE,
)

# Adapter-side crash before any model call: an uncaught Python traceback
# (e.g. FileNotFoundError for a missing harness binary) is infrastructure —
# but only when there is no evidence a model ran. Agents debugging Python
# tasks legitimately print tracebacks in their transcripts, so these markers
# must never reclassify a cell that shows real work (tokens or turns).
_ADAPTER_CRASH_RE = re.compile(
    r"Traceback \(most recent call last\)|FileNotFoundError",
    re.IGNORECASE,
)

_TIMEOUT_RE = re.compile(r"\b(?:timeout|timed out)\b", re.IGNORECASE)


def _text(*parts):
    return "\n".join(str(p) for p in parts if p is not None)


def has_rate_limit_marker(text):
    """Return True when CLI output contains a high-confidence rate-limit signature."""
    return any(rx.search(text or "") for rx in _RATE_LIMIT_RES)


def has_infra_marker(text):
    """Return True when row/error/output text indicates harness infrastructure."""
    return bool(_INFRA_RE.search(text or ""))


def has_instant_cli_exit_shape(row):
    """Return True for near-instant adapter CLI exits with no model traffic.

    The post-run gate uses the same predicate as a drift guard. The contract is
    deliberately narrow and structural: a bare ``exit N`` before 30 seconds with
    no aggregate token count is a local CLI/configuration failure, not a task
    wrong answer.
    """
    row = row or {}
    if bool(row.get("completed")):
        return False
    if not re.match(r"^exit \d+$", str(row.get("error") or "")):
        return False
    if row.get("tokens") not in (None, 0):
        return False
    wall = row.get("wall_time_s")
    if not isinstance(wall, (int, float)) or isinstance(wall, bool):
        return False
    return float(wall) < 30.0


def has_zero_output_and_minimal_turns(row, text=""):
    """True when a completed cell produced no model output and at most 1 turn.

    A harness that started but returned zero output tokens in its only turn
    (or zero turns) is infrastructure: the model never produced a useful
    response.  This prevents cells that complete in a few seconds with no
    output tokens from being misclassified as ``wrong_answer`` (the model
    couldn't have answered wrong if it never answered).

    The canonical pattern: gpt-5.6-sol cells with 2--9s wall time,
    0 tokens_output, 1 turn, untouched workspace.

    Pass ``text`` (adapter output) to suppress the gate when meaningful
    model/adapter text exists despite zero reported tokens — some adapters
    print diagnostic output on stdout without going through the token parser.
    """
    row = row or {}
    if row.get("harness") == "null":
        return False
    if not bool(row.get("completed")):
        return False
    if bool(row.get("success")):
        return False
    # Check tokens_output specifically — zero model output is the signal.
    out = row.get("tokens_output")
    if out not in (None, 0):
        return False
    # Also check aggregate tokens to catch proxy-only token fields.
    if row.get("tokens") not in (None, 0):
        return False
    # At most 1 turn — a single probe-turn that produced nothing is infra.
    turns = row.get("turns")
    if turns is None or (isinstance(turns, (int, float)) and turns > 1):
        return False
    # Suppress gate when there's meaningful output text despite zero tokens.
    if text and len(_meaningful_work_text(text)) >= 10:
        return False
    return True


def _wall_rode_cap(row, timeout_s):
    if timeout_s is None:
        timeout_s = row.get("timeout_s")
    if not timeout_s:
        return False
    wall = row.get("wall_time_s")
    if not isinstance(wall, (int, float)) or isinstance(wall, bool):
        return False
    try:
        cap = float(timeout_s)
    except (TypeError, ValueError):
        return False
    if cap <= 0:
        return False
    return float(wall) >= (cap * 0.98)


def _meaningful_work_text(text):
    """Return text evidence after dropping generic transcript/timeout boilerplate."""
    kept = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.fullmatch(r"[-=]{3,}", line):
            continue
        if re.fullmatch(r"(?:transcript|output|stdout|stderr|tail|error|metadata)\s*:?", line, re.IGNORECASE):
            continue
        if re.fullmatch(r"(?:timeout|timed out)(?: after)? \d+(?:\.\d+)?s?", line, re.IGNORECASE):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def has_near_zero_agent_tokens(row):
    """True when every reported agent/proxy token field is absent or below 100."""
    values = [row.get(field) for field in _TOKEN_FIELDS]
    return all(value in (None, 0) or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and 0 <= value < NEAR_ZERO_TOKEN_LIMIT
    ) for value in values)


def _has_workspace_work_evidence(row):
    """Recognize explicit change summaries without treating pristine file lists as work."""
    if row.get("workspace_changed") is True:
        return True
    changes = row.get("workspace_changes")
    return bool(changes) if isinstance(changes, (dict, list, tuple, set, str)) else False


def _has_model_work_evidence(row, text):
    """Conservative evidence that a model ran despite missing token telemetry.

    Token parsers can fail for otherwise healthy adapters.  Turns, meaningful
    model/adapter output (10+ characters), or explicit workspace-change evidence
    therefore prevent the silent-failure reclassification.  Merely listing the
    checker workspace is not evidence because it includes pristine task files.
    """
    return (not has_near_zero_agent_tokens(row) or bool(row.get("turns"))
            or len(_meaningful_work_text(text)) >= 10
            or _has_workspace_work_evidence(row))


def _has_no_work_evidence(row, text):
    # Cap-rider handling predates the completed-run heuristic and intentionally
    # treats any nonzero aggregate token report as evidence of work.
    return not row.get("tokens") and not row.get("turns") and len(_meaningful_work_text(text)) < 200


def is_silent_no_model_call(row, adapter_output=""):
    """True for a completed, unsolved cell with no evidence the model ran.

    The zero-work classifier gate (``has_zero_output_and_minimal_turns``) is
    checked here for symmetry — a cell that produced zero output tokens in <=1
    turn is infra regardless of whether near-zero aggregate tokens or turns=1
    happens to satisfy the broader ``_has_model_work_evidence`` heuristic.
    """
    row = row or {}
    text = _text(adapter_output, row.get("output_tail"), row.get("error"))
    return (row.get("harness") != "null"
            and bool(row.get("completed")) and not bool(row.get("success"))
            and (has_zero_output_and_minimal_turns(row, text)
                 or not _has_model_work_evidence(row, text)))


def has_checker_crash(row):
    """True when the checker itself failed to execute (not a graded verdict).

    A checker communicates its verdict through exit 0 (pass) / 1 (fail), or the
    literal ``"timeout"`` sentinel. Any other exit code means the checker never
    reached a verdict — docker refusing to start it (125/126/127), a signal, or
    an interpreter crash. Those cells measure our infrastructure, not the model,
    so they must never be scored as ``wrong_answer``.
    """
    exit_code = (row or {}).get("checker_exit")
    if exit_code is None or exit_code == "timeout":
        return False
    try:
        return int(exit_code) not in (0, 1)
    except (TypeError, ValueError):
        return False


def classify_failure_reason(row, adapter_output=""):
    """Return a stable diagnostic reason without overriding stronger markers."""
    row = row or {}
    combined = _text(adapter_output, row.get("output_tail"), row.get("error"),
                     row.get("checker_exit"))
    if (bool(row.get("success")) or has_rate_limit_marker(combined)
            or has_infra_marker(combined) or has_instant_cli_exit_shape(row)):
        return None
    if is_silent_no_model_call(row, adapter_output):
        return "silent-no-model-call"
    return None


def classify_failure(row, adapter_output="", timeout_s=None):
    """Classify a benchmark row into the self-auditing failure taxonomy.

    ``adapter_output`` should be the full adapter stdout+stderr at write time;
    callers that only have an old row may pass ``output_tail`` instead.
    """
    row = row or {}
    combined = _text(
        adapter_output,
        row.get("output_tail"),
        row.get("error"),
        row.get("checker_exit"),
    )
    structured_status = _text(row.get("error"), row.get("checker_exit"))

    if bool(row.get("success")):
        return "solved"
    if has_rate_limit_marker(combined):
        return "rate_limited"
    if has_infra_marker(combined):
        return "infra"
    if (_ADAPTER_CRASH_RE.search(combined)
            and has_near_zero_agent_tokens(row) and not row.get("turns")):
        return "infra"
    if has_instant_cli_exit_shape(row):
        return "infra"
    if is_silent_no_model_call(row, adapter_output):
        return "infra"
    if has_checker_crash(row):
        return "infra"
    # Wall time riding the cap only means "timeout" when the runner killed the
    # agent; a CLI that exited on its own (completed=True) just ran slow.
    rode_cap = _wall_rode_cap(row, timeout_s) and not bool(row.get("completed"))
    # Cap-riding with zero work evidence (no tokens, no turns, empty transcript)
    # is a silent retry loop or hang, not a capability timeout.
    if rode_cap and _has_no_work_evidence(row, combined):
        return "infra"
    if row.get("checker_exit") == "timeout" or _TIMEOUT_RE.search(structured_status) or rode_cap:
        return "timeout"
    return "wrong_answer"


def class_for_report(row):
    """Return a row's failure_class, deriving one from saved fields if absent."""
    fc = (row or {}).get("failure_class")
    if fc in FAILURE_CLASSES:
        return fc
    return classify_failure(row, (row or {}).get("output_tail") or "")


def is_excluded_from_solve_rate(row):
    return class_for_report(row) in EXCLUDED_FROM_SOLVE_RATE
