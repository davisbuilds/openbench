"""Canonical usage-evidence grades and ranking policy."""

from __future__ import annotations

from typing import Any, Mapping

GRADE_HARBOR_REPORTED = "harbor_reported"
GRADE_PROXY_VERIFIED = "harbor_reported_proxy_verified"
GRADE_PROXY_MISMATCH = "harbor_reported_proxy_mismatch"
GRADE_UNAVAILABLE = "usage_unavailable"

# Matrix/legacy-runner grades. That path does not have Harbor's agent-reported
# usage + reconciliation; its evidence is the counting proxy's independent meter,
# the adapter's own vendor token split, or a token estimate -- so it grades on
# that vocabulary. GRADE_UNAVAILABLE is shared with the Harbor grades above.
GRADE_PROXY_MEASURED = "proxy_measured"
GRADE_VENDOR_REPORTED = "vendor_reported"
GRADE_ESTIMATED = "estimated"

GRADES = frozenset({
    GRADE_HARBOR_REPORTED,
    GRADE_PROXY_VERIFIED,
    GRADE_PROXY_MISMATCH,
    GRADE_UNAVAILABLE,
    GRADE_PROXY_MEASURED,
    GRADE_VENDOR_REPORTED,
    GRADE_ESTIMATED,
})

LABELS = {
    GRADE_HARBOR_REPORTED: "Harbor-reported",
    GRADE_PROXY_VERIFIED: "Harbor-reported + proxy-verified",
    GRADE_PROXY_MISMATCH: "Harbor/proxy mismatch",
    GRADE_UNAVAILABLE: "Usage unavailable",
    GRADE_PROXY_MEASURED: "Proxy-measured",
    GRADE_VENDOR_REPORTED: "Vendor-reported",
    GRADE_ESTIMATED: "Estimated (excluded)",
}

EXCLUSION_PROXY_MISMATCH = "proxy_mismatch"
EXCLUSION_USAGE_UNAVAILABLE = "usage_unavailable"
EXCLUSION_USAGE_ESTIMATED = "usage_estimated"

# Grades that never contribute to token/cost/efficiency metrics, whatever the
# row's explicit ranking flag says (they fail closed in ranking_eligible).
_NOT_RANKABLE_GRADES = frozenset({
    GRADE_PROXY_MISMATCH,
    GRADE_UNAVAILABLE,
    GRADE_ESTIMATED,
})


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


def matrix_usage_policy(
    token_basis: Any,
    *,
    proxy_measured: bool,
) -> tuple[str, bool, str | None]:
    """Return ``(grade, ranking_eligible, exclusion_reason)`` for a matrix-runner row.

    The matrix/legacy runner grades usage from what it actually captured, best
    evidence first: the counting proxy's independent meter (``proxy_measured``,
    strongest and authoritative regardless of the adapter's own accounting), else
    the adapter's vendor token split (``token_basis == "vendor_split"``, the
    vendor's own reported counts), else a token estimate
    (``token_basis == "estimated"`` -- present but excluded from ranking so a
    guess never drives cost/token metrics). Everything else (no meter, an
    unmetered BYO candidate, a failed cell with no usage) is unavailable.

    Distinct from ``harbor_usage_policy``, which grades Harbor-agent-reported
    usage against proxy reconciliation.
    """
    if proxy_measured:
        return GRADE_PROXY_MEASURED, True, None
    if token_basis == "vendor_split":
        return GRADE_VENDOR_REPORTED, True, None
    if token_basis == "estimated":
        return GRADE_ESTIMATED, False, EXCLUSION_USAGE_ESTIMATED
    return GRADE_UNAVAILABLE, False, EXCLUSION_USAGE_UNAVAILABLE


def ranking_eligible(row: Mapping[str, Any]) -> bool:
    """Whether a row may contribute to token, cost, or efficiency metrics.

    Rows predating this policy remain eligible. Mismatch, unavailable, and
    estimated grades fail closed even if a caller omitted the boolean field.
    """
    grade = row.get("usage_evidence_grade")
    if grade in _NOT_RANKABLE_GRADES:
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
