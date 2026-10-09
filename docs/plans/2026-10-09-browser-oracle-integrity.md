---
status: implemented
---

# Browser oracle integrity and acceptance v2

The v1 audit reproduced two presentation false negatives and a measurement
bypass: page-world overrides hid actual clipping from the grader. Original
results and v1 replay remain available; they are not rewritten as v2 results.

## This slice

1. Add a v2 worker that reads browser-owned accessibility nodes and measures
   them in a dedicated Chromium isolated execution world. Candidate page
   prototypes/globals must not control measurements, ordering, focus, text, or
   serialization. Preserve the container/egress/solver-stop boundary.
2. Accept either a labeled region or dialog for details, with cosmetic text
   casing normalized while required content and visibility remain checked.
   Keep the explicit minimum target size and clipping/scrolling requirements.
3. Add independently implemented modal/transformed-text controls and deliberate
   measurement/order/focus/content spoofing controls. Demonstrate the known v1
   false positive and v2 rejection in the same real browser runtime.
   Retry must be reached through Tab and activated through Enter; pointer-only
   and keyboard-blocked retry controls must fail while valid layouts pass.
4. Register activity-explorer-c2-o2, keep c1-o1 historical, quarantine v1 from
   quality admission, and update runtime, import, review and CI consumers.
5. Retain bucket score compatibility but add per-check failure reasons and a
   check pass fraction; neither is a design-preference score. Replay frozen
   pilot assets as separately identified diagnostics after controls pass.
6. Run required checks, open a PR and request Codex review. Do not launch scored
   model attempts in this slice.

Private scored holdout packaging remains a separate architecture decision.
Only public synthetic calibration fixtures belong in this implementation;
private artifacts, identity keys and review captures stay outside Git.

## Evidence gates

- Multiple correct layouts pass, including a dialog with CSS-transformed text.
- The demonstrated width/range override cannot hide clipping; valid pages with
  benign page-world overrides still produce correct observations.
- Ordering, focus and text observations are independent of main-world overrides.
- Failed or destroyed observer contexts fail closed; never fall back to page
  evaluation. Original v1 replay behavior is retained.
- Historical browser oracles cannot authorize a new campaign. Actual sandbox
  controls and canonical Harbor receipt import cover the new oracle route.
