# Harness-bloat ablation — Codex config rungs vs pi, GPT-5.6 Sol (2026-07-12)

**Hypothesis (Matthew's):** Codex's fixed harness context (~12k tokens/turn vs
pi's ~1.5k) hurts token efficiency and latency without buying correctness.
Tested by config-only ablation — no code changes, each variant is stock
codex-cli 0.144.1 plus a `CODEX_HOME` config (`ablation/codex-home-v*`).

## Design

- **V0** stock codex · **V1** 200-word base prompt via `model_instructions_file`
  · **V2** V1 + permissions/env/apps/skills/collab blocks off +
  `project_doc_max_bytes=4096` · **pi** minimal-harness reference.
- 15 gated tasks × 5 trials × 4 groups = 300 cells, GPT-5.6 Sol, sequential
  docker cells (`--cpus 4`) on Matthews-Mac-mini, pinned image `9aa0f24b…`
  (in-container CLI versions stamped per row). PROVENANCE: OK across groups.
- Measured fixed context per turn (request-payload capture): V0 11.7k tokens →
  V1 7.4k → V2 4.5k → pi 1.5k. Tool schemas (4.3k) are the config-unreachable
  floor for codex.
- 75 pi cells were rerun after an OAuth-rotation auth failure (classified
  infra, struck from the file); 3 rows excluded (2 infra, 1 rate_limited).

## Results (matched cells, n=72/group)

| Group | Solve raw | Solve hack-adj | Wilson 95% | Wall/solve | Input processed/solve* |
|---|---:|---:|---|---:|---:|
| codex V0 | 79.2% | **73.6%** | [0.62,0.82] | 47.3s | 91.6k |
| codex V1 | 73.6% | 73.6% | [0.62,0.82] | 33.3s | 64.4k |
| codex V2 | 77.8% | **77.8%** | [0.67,0.86] | 36.4s | **58.5k** |
| pi | 73.6% | 73.6% | [0.62,0.82] | 41.9s | **22.2k** |

*median uncached + cache-read input over the 50 cells all four groups solved.

## Findings

1. **De-bloating costs no correctness.** V2 ≈ V0 raw (77.8 vs 79.2, CIs
   overlap); hack-adjusted, V2 is nominally highest. Processes **36% less
   input** and solves **~25% faster** than stock.
2. **The bloat hides in cache-reads.** Codex's fixed prompt is cached after
   turn 1, so blended token counts mask it; the uncached/cache-read split
   (`usage_raw`) shows stock codex re-processing ~76k cached tokens per solve
   vs V2's 46k and pi's 10k.
3. **The stock prompt correlates with reward hacking.** On schemelike, stock
   codex hacked 4/5 solves (self-host collapse — same trick as the variant
   study); V1, V2, and pi: **0 clear hacks** (one V1 gray). Grep-verified:
   collapse narration appears in exactly the 4 flagged stock transcripts and
   nowhere else. n=5/group — treat as a strong lead, not a law.
4. **pi's residual edge is the schema floor.** pi does the same work with 904
   schema tokens vs codex's 4.3k (config-unreachable) — the case for a V3
   fork rung if pursued.
5. This matrix ran on the PRE-hardening schemelike checker (hardening merged
   mid-run); the 4 stock hacks passed it. The hardened checker (mutation
   hardening, certified 20/20 on the mini) makes the collapse fail outright
   in future runs.

## Files

- `ablation-sol-n5.jsonl` — scrubbed results (300 rows).
- `hack-report-ablation.md` — sweep report (orchestrator-verified headline).
- Variant configs: `ablation/codex-home-v{0,1,2}/` in the repo.
- Measurement spike: `ablation/MEASUREMENT.md`.
