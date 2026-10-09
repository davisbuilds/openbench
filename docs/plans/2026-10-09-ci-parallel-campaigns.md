---
status: in-progress
---

# CI throughput and two-trial campaigns

## Agreed scope

- Set the prepared UI pilot to 1,800 seconds per attempt; preserve the general
  timeout and historical qualification suites.
- Run two benchmark trials when host and Docker capacity permit, with a recorded
  serial fallback before qualification/launch. Keep concurrency fixed per run.
- Reduce hosted CI's critical path without removing independent grading replay.
- Qualification controls are authorized; comparative task attempts remain a
  separate launch.

## Delivery

1. PR #31 splits the measured 24m43s Harbor job into repair, browser and
   concurrency-control runners. Every previous command remains; `sandbox` is
   an all-lanes aggregate. Codex review reported no major issues. Hosted Actions
   rejected dispatch as disabled; actual speedup remains unmeasured.
2. Add capacity-aware suite preparation, exact one/two-trial admission, paired
   sandbox lifecycle controls, and observed authenticated execution overlap.
   Ordinary harness OAuth concurrency remains unchanged.
3. Pass offline regressions, review and fresh Mini qualification before using
   two-trial execution. Preserve failed attempts and all qualification evidence.

## Verification and limits

Capacity samples and decisions are local evidence. Missing data selects serial;
changed capacity at dispatch requires new prepared intent. This is neither a
continuous scheduler nor a resource reservation. Provider quota is independent.

A configured concurrency value is insufficient evidence: the paired control
must preserve separate writes and survive peer cleanup, and live Harbor trials
must actually overlap. Existing task-quality replay remains mandatory. Do not
infer calibrated task difficulty from these controls.
