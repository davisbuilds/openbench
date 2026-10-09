# Activity explorer — case 2, oracle 2

Synthetic product-implementation task with a small static starter and a supplied
JSON endpoint. It uses no personal activity records or captured repository data.
This is a calibration candidate; its frontier difficulty is unknown.

The [public contract](instruction.md) allows independent designs. Only `web/`
assets are submitted. The evaluator serves them directly; it does not execute
candidate package scripts. This first slice measures browser behavior rather
than React/framework/build-system fluency.

## Scoring and controls

The host-owned `activity-explorer-v2` oracle compares browser observations with
expected behavior. Four equally weighted buckets cover functionality, request
states, keyboard navigation, and responsive layout. A bucket passes only when
all its checks pass; complete success requires every check. Partial scores are
coverage summaries, not estimates of the percentage of implementation completed.

Controls live outside the task package in
`scripts/ci/browser_quality_fixtures.py`. A list, card layout, and independently implemented modal must pass, including
CSS-transformed metadata. Benign page-world overrides must not alter verdicts.
Deliberate defects cover filtering, ordering, duplication, retry, keyboard access,
clipped/hidden content, nested scrolling, and oversized details. Quality admission
requires fresh replay plus a matching actual-harness workflow receipt. Grading
never mounts the controls or host verdict code into the solver.

Screenshots support a separate human assessment of hierarchy, spacing, visual
coherence and preference. The deterministic checks do not certify accessibility,
visual taste, full specification coverage, or resistance to every intentionally
adversarial browser technique. Keyboard checks include opening and closing details; manual review should also
inspect focus appearance and retry navigation. Browser-owned accessibility nodes
and a dedicated isolated JavaScript world protect measurements from page-world
prototype overrides. This is a tested boundary, not a universal anti-cheating
claim. Public synthetic controls are calibration material, not private holdouts.

## Runtime and operator checks

Use the [browser runtime](../../../../docker/browser-runtime/README.md), pinned by
immutable image ID. `obench campaign qualify` recognizes this task's browser
profile and requires browser controls in addition to normal repair isolation.
A repair-only admission cannot authorize this profile on the same image.

From the repository root, with the pinned optional Harbor environment active:

```sh
python scripts/local/build_browser_runtime.py --base-image sha256:BASE_IMAGE_ID --receipt results/browser/runtime.json
python scripts/local/verify_repair_codex.py --runtime-image sha256:BROWSER_IMAGE_ID --task benchmarks/harbor/local/activity-explorer-c2-o2 --model gpt-6.1-sol-high --project-check 'pnpm build' --output-dir results/browser/workflow
python scripts/local/verify_browser_quality.py --image sha256:BROWSER_IMAGE_ID --output results/browser/quality --workflow results/browser/workflow/receipt.json
python scripts/local/verify_browser_lifecycle.py --runtime-image sha256:BROWSER_IMAGE_ID --output results/browser/lifecycle
```

Use new evidence paths for every attempt. These controls are offline and make
no model inference requests. The actual Codex process runs tools against a local
synthetic provider; distinct before/after screenshots must reach that provider
byte-for-byte. This proves image transport, not a model's visual understanding.
Fresh execution-host qualification and current task-quality evidence are still
required before comparative trials.

## Revision

Case 2 permits either a labeled region or dialog and cosmetic capitalization.
Oracle 2 protects browser observations, checks keyboard close, normalizes cosmetic
text casing, and reports per-check failure reasons and check-pass fraction alongside
the unchanged four strict buckets. Original v1 results are preserved; v1 cannot
authorize new quality admission. Saved assets may be replayed as separate v2
diagnostics, never relabeled as fresh attempts.
