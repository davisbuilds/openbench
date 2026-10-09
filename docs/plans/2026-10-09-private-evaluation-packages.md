---
status: implemented
---

# Private evaluation packages

Keep reusable execution/verification infrastructure and synthetic calibration
controls public. Private owners retain scored tasks, hidden data, reference
repairs, grading rules and detailed results. Moving a previously public case
does not make it an unseen holdout.

## First delivery: package boundary and offline replay

- Freeze a bounded, deterministic archive with separate solver, evaluator,
  fixture and control namespaces. Bind case and oracle revisions independently.
- Require an operator-supplied archive and SHA256. Never resolve evaluators from
  candidate metadata, environment variables or implicit filesystem discovery.
- Inspect/export without executing evaluator code. Export only the explicit
  solver projection. The owner must review that projection for intended content.
- Require explicit evaluator trust for replay: author Python executes with host
  authority. Its file bytes come from the verified archive snapshot. Only a
  closed public worker backend runs candidate code in the isolated container.
- Persist diagnostic replay identity, source hashes, implementation hashes,
  bounded failed checks and operation status. Refuse malformed grader results.
- Prove mutation rejection, non-executing inspection, export isolation and
  positive/negative controls in actual Docker. No live model calls.

This slice does not issue campaign-quality or runtime-admission receipts and
does not register private evaluators with the campaign runner. Existing campaign
gates remain closed to these packages.

Implemented by `obench repair package` and documented in
[the package contract](../private-evaluation-packages.md). Offline contract tests
cover pinning, safe export, explicit trust and malformed verdicts. The browser
Docker CI lane exercises real private-rule replay plus solver workspace/host
archive controls using wholly synthetic public inputs. Production integration
below remains a separate delivery.

## Next delivery: campaign integration

Thread the explicit package pin through suite compilation, Harbor environment
and verifier kwargs, task identity, quality/workflow/runtime admission, replay,
sealed job/result import and historical result readers. Prove fresh authoring
and solver sessions are separate and actual harness direct/symlink/subprocess
reads of known-present private assets are denied. Do not authorize scored runs
until these consumers agree and authoritative admission independently replays
the current controls.

## Authoring policy

Keep development and held-out comparison cases separate. Exposure to tuning or
debugging retires a held-out case into development. Version frozen inputs and
record provenance privately. First package controls establish the boundary,
not frontier difficulty. No private repository names, local paths, case contents
or results belong in public implementation commits or PR discussions.
