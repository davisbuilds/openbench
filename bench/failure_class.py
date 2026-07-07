#!/usr/bin/env python3
"""Failure taxonomy shared by the runner, reports, and backfill tool.

The runner classifies rows at write time using the adapter's full untruncated
output when adapters provide ``full_output``. Backfill can only use fields saved
in old JSONL rows (usually ``output_tail``), so old-file detection is inherently
weaker for markers that were truncated away.
"""

import re

FAILURE_CLASSES = ("solved", "wrong_answer", "timeout", "rate_limited", "infra")
EXCLUDED_FROM_SOLVE_RATE = ("rate_limited", "infra")

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
    r"not logged in|login required|please log in|"
    r"No such image:\s*[^\s]+|"
    r"image ['\"]?[^'\"\n]+['\"]? not found|"
    r"Cannot connect to the Docker daemon"
    r")",
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


def _wall_rode_cap(row, timeout_s):
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
    if row.get("checker_exit") == "timeout" or _TIMEOUT_RE.search(structured_status) or _wall_rode_cap(row, timeout_s):
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
