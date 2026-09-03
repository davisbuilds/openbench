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
    native_tokens_present: bool = False,
) -> tuple[str, bool, str | None]:
    """Return ``(grade, ranking_eligible, exclusion_reason)`` for a matrix-runner row.

    The grade must name the *source the consumers actually rank*, not the best
    evidence the cell captured. ``stats.effective_tokens`` /
    ``stats.input_tokens`` / ``stats.output_tokens`` and ``compare._measurement``
    all prefer the adapter's own native token scalars and consult the proxy meter
    only as a fallback when those are absent. So:

    - ``native_tokens_present`` (the adapter reported its own token scalars, which
      the consumers select first): grade the adapter's basis -- ``vendor_split``
      (or a bare scalar) is the vendor's reported counts (``vendor_reported``,
      eligible); an ``estimated`` scalar is a guess that is selected before the
      proxy yet excluded from ranking (``estimated``). A proxy meter that also
      fired does NOT upgrade the grade, because its number is not the one ranked
      -- stamping ``proxy_measured`` here would claim independent proxy provenance
      for a vendor-reported figure.
    - Otherwise, no native scalar is present and the consumers fall back to the
      proxy: ``proxy_measured`` (eligible) if it fired, else ``estimated`` for a
      bare estimate basis, else unavailable (no meter, an unmetered BYO
      candidate, or a failed cell with no usage).

    Distinct from ``harbor_usage_policy``, which grades Harbor-agent-reported
    usage against proxy reconciliation.
    """
    if native_tokens_present:
        if token_basis == "estimated":
            return GRADE_ESTIMATED, False, EXCLUSION_USAGE_ESTIMATED
        return GRADE_VENDOR_REPORTED, True, None
    if proxy_measured:
        return GRADE_PROXY_MEASURED, True, None
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
