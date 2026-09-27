# Provenance — dojo-evidence-pr60-v5

Revision 5 preserves the v4 source snapshot and solver instruction byte-for-byte.
The September 2026 screening audit found that the budget comparator rejected an
unsupported, non-gating assessment solely for retaining diagnostic entry counts.
The instruction does not require zeroing those counts. V5 accepts either zeroed
or retained counts while requiring the unsupported verdict, false gating, and
public field types. Positive-mode and all other behavioral checks are unchanged.

V4 results retain their original scores. Regrading saved submissions under v5 is
an oracle audit, not fresh model evidence; confirmation uses new attempts.
Source ownership and original PR/commit provenance are recorded in
[the v4 provenance](../dojo-evidence-pr60-v4/PROVENANCE.md).
