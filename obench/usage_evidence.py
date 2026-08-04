"""Canonical usage-evidence grades and ranking policy."""

from __future__ import annotations

from typing import Any, Mapping

GRADE_HARBOR_REPORTED = "harbor_reported"
GRADE_PROXY_VERIFIED = "harbor_reported_proxy_verified"
GRADE_PROXY_MISMATCH = "harbor_reported_proxy_mismatch"
GRADE_UNAVAILABLE = "usage_unavailable"

GRADES = frozenset({
    GRADE_HARBOR_REPORTED,
    GRADE_PROXY_VERIFIED,
    GRADE_PROXY_MISMATCH,
    GRADE_UNAVAILABLE,
})

LABELS = {
    GRADE_HARBOR_REPORTED: "Harbor-reported",
    GRADE_PROXY_VERIFIED: "Harbor-reported + proxy-verified",
    GRADE_PROXY_MISMATCH: "Harbor/proxy mismatch",
    GRADE_UNAVAILABLE: "Usage unavailable",
}

EXCLUSION_PROXY_MISMATCH = "proxy_mismatch"
EXCLUSION_USAGE_UNAVAILABLE = "usage_unavailable"


def harbor_usage_policy(
    token_basis: Any,
    *,
    proxy_required: bool,
    reconciliation_status: Any = None,
) -> tuple[str, bool, str | None]:
    """Return ``(grade, ranking_eligible, exclusion_reason)`` for a Harbor row."""
    if token_basis != "harbor_agent_reported":
        return GRADE_UNAVAILABLE, False, EXCLUSION_USAGE_UNAVAILABLE
    if not proxy_required:
        return GRADE_HARBOR_REPORTED, True, None
    if reconciliation_status == "exact":
        return GRADE_PROXY_VERIFIED, True, None
    if reconciliation_status == "mismatch":
        return GRADE_PROXY_MISMATCH, False, EXCLUSION_PROXY_MISMATCH
    return GRADE_UNAVAILABLE, False, EXCLUSION_USAGE_UNAVAILABLE


def ranking_eligible(row: Mapping[str, Any]) -> bool:
    """Whether a row may contribute to token, cost, or efficiency metrics.

    Rows predating this policy remain eligible. Explicit mismatch and unavailable
    grades fail closed even if a caller omitted the boolean field.
    """
    grade = row.get("usage_evidence_grade")
    if grade in (GRADE_PROXY_MISMATCH, GRADE_UNAVAILABLE):
        return False
    return row.get("usage_ranking_eligible") is not False


def display_label(row: Mapping[str, Any]) -> str | None:
    grade = row.get("usage_evidence_grade")
    return LABELS.get(grade)


def exclusion_reason(row: Mapping[str, Any]) -> str | None:
    if ranking_eligible(row):
        return None
    reason = row.get("usage_ranking_exclusion_reason")
    return str(reason) if reason not in (None, "") else EXCLUSION_USAGE_UNAVAILABLE
