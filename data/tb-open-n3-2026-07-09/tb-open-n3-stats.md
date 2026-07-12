# tb-open-n3 statistics report

Generated 2026-07-09. Dataset: 180 rows (176 valid for solve-rate denominators after excluding rate_limited/infra).

> **QUARANTINE NOTE (added 2026-07-10):** `cancel-async-tasks` rows are under
> quarantine — its checker was proven non-deterministic under load (hardcoded
> 0.5s SIGINT + 5s exit deadline; hash-verified graded-FAIL bytes pass on
> rerun). Its per-task solve rates are grader noise. The Layer-1 matched-cell
> headlines below are largely unaffected (strict cells mostly exclude it);
> treat any cancel-async-specific number as invalid pending checker fix +
> rerun. Details: tb-open-n3-methodology-notes.md "QUARANTINE" section.

## 1. Headline — Layer 1 matched-cell efficiency

Strict rule for this headline: for each metric, first select task×model cells solved by every harness in the comparison set, then further restrict to cells where every harness has that metric available. Per-cell medians use telemetry-AVAILABLE solving trials for that metric; the complete-telemetry dollar sensitivity below shows the stricter alternative. The headline aggregate is the median of those same cell medians. Speed uses only under-cap solved trials (wall_time_s < 1176). Tokens use tokens_fresh only. Dollars use only rows with input/cache/output split fields priceable from the dated vendor table.

### Strict headline A — all five harnesses
| Harness | tokens_fresh/solve (6 strict cells) | s/solve under-cap (5 strict cells) |
| --- | --- | --- |
| pi | 63058 | 443.15 |
| opencode | 63068 | 644.84 |
| claude | 72350 | 394.99 |
| codex | 77232 | 713.71 |
| grokbuild | 71448 | 635.68 |
Solved-by-all base cells (7): deepseek-v4-flash × cancel-async-tasks; deepseek-v4-flash × llm-inference-batching-scheduler; glm-5.2 × feal-differential-cryptanalysis; glm-5.2 × llm-inference-batching-scheduler; kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler; kimi-k2.7-code × schemelike-metacircular-eval.
tokens_fresh strict cells (6): deepseek-v4-flash × cancel-async-tasks; deepseek-v4-flash × llm-inference-batching-scheduler; glm-5.2 × feal-differential-cryptanalysis; glm-5.2 × llm-inference-batching-scheduler; kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler.
under-cap wall strict cells (5): deepseek-v4-flash × cancel-async-tasks; deepseek-v4-flash × llm-inference-batching-scheduler; glm-5.2 × feal-differential-cryptanalysis; kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler.

All-5 priced sensitivity (demoted; Kimi-only, 2 cells):
| Harness | $/solve (2 cells) |
| --- | --- |
| pi | $0.252 |
| opencode | $0.266 |
| claude | $0.235 |
| codex | $0.434 |
| grokbuild | $0.299 |
cells (2): kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler.
This is not the primary priced headline: grokbuild's deepseek/glm scalar_exact rows lack input/output splits, so all-5 pricing collapses to Kimi-only cells.

### Strict headline B — core four harnesses (pi/opencode/claude/codex)
| Harness | tokens_fresh/solve (7 strict cells) | s/solve under-cap (7 strict cells) | $/solve primary priced comparison (5 strict cells) |
| --- | --- | --- | --- |
| pi | 72883 | 466.33 | $0.164 |
| opencode | 66536 | 747.14 | $0.265 |
| claude | 68679 | 525.59 | $0.135 |
| codex | 99995 | 757.09 | $0.227 |
Solved-by-all base cells (8): deepseek-v4-flash × cancel-async-tasks; deepseek-v4-flash × feal-differential-cryptanalysis; deepseek-v4-flash × llm-inference-batching-scheduler; glm-5.2 × feal-differential-cryptanalysis; glm-5.2 × llm-inference-batching-scheduler; kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler; kimi-k2.7-code × schemelike-metacircular-eval.
tokens_fresh strict cells (7): deepseek-v4-flash × cancel-async-tasks; deepseek-v4-flash × feal-differential-cryptanalysis; deepseek-v4-flash × llm-inference-batching-scheduler; glm-5.2 × feal-differential-cryptanalysis; glm-5.2 × llm-inference-batching-scheduler; kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler.
under-cap wall strict cells (7): deepseek-v4-flash × cancel-async-tasks; deepseek-v4-flash × feal-differential-cryptanalysis; deepseek-v4-flash × llm-inference-batching-scheduler; glm-5.2 × feal-differential-cryptanalysis; glm-5.2 × llm-inference-batching-scheduler; kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler.
price strict cells (5): deepseek-v4-flash × cancel-async-tasks; glm-5.2 × feal-differential-cryptanalysis; glm-5.2 × llm-inference-batching-scheduler; kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler.

Headline read (strict, descriptive): all-5 numerically lowest tokens_fresh/solve in this sample is **pi** (63058); this is a near-tie with opencode (63068), a 10-token / 0.016% gap. All-5 numerically fastest under-cap s/solve is **claude** (394.99s). Core-4 numerically lowest tokens_fresh/solve is **opencode** (66536); core-4 numerically fastest is **pi** (466.33s). Core-4 primary priced comparison is telemetry-censoring-sensitive: claude is numerically lowest under available-trial telemetry ($0.135), but the complete-telemetry sensitivity below flips the $ winner to opencode.
Layer-1 rankings are descriptive medians over 5–7 cells, not statistically separated.

Telemetry-censoring sensitivity for dollars: the headline rule uses medians over telemetry-available solving trials within included cells, not necessarily every solved trial in that cell. If instead every solved trial in an included cell is required to have priceable telemetry, all-5 $ collapses to 1 Kimi-only cell and core-4 $ collapses to 4 cells; in both cases the numerical $ winner flips from claude to opencode.
All-5 complete-price-telemetry sensitivity:
| Harness | $/solve (1 cells) |
| --- | --- |
| pi | $0.340 |
| opencode | $0.265 |
| claude | $0.334 |
| codex | $0.694 |
| grokbuild | $0.344 |
cells (1): kimi-k2.7-code × llm-inference-batching-scheduler.
Core-4 complete-price-telemetry sensitivity:
| Harness | $/solve (4 cells) |
| --- | --- |
| pi | $0.219 |
| opencode | $0.168 |
| claude | $0.208 |
| codex | $0.297 |
cells (4): deepseek-v4-flash × cancel-async-tasks; glm-5.2 × feal-differential-cryptanalysis; glm-5.2 × llm-inference-batching-scheduler; kimi-k2.7-code × llm-inference-batching-scheduler.

### Secondary availability view — not a headline
The table below preserves the previous matched-cell coverage view over the all-5 solved-cell set (7 cells: deepseek-v4-flash × cancel-async-tasks; deepseek-v4-flash × llm-inference-batching-scheduler; glm-5.2 × feal-differential-cryptanalysis; glm-5.2 × llm-inference-batching-scheduler; kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler; kimi-k2.7-code × schemelike-metacircular-eval). It is useful for telemetry coverage and cap-rider counts, but should not be used as the headline ranking because metric coverage differs by harness.

| Harness | available-cell tokens_fresh/solve | token cov | available-cell s/solve | speed cov | cap riders | available-cell $/solve | $ cov |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pi | 82905 | 17/18 | 454.74 | 17/18 | 1 | $0.252 | 15/18 |
| opencode | 66536 | 18/18 | 695.99 | 14/18 | 4 | $0.265 | 18/18 |
| claude | 72350 | 13/14 | 499.73 | 13/14 | 1 | $0.108 | 13/14 |
| codex | 77232 | 14/16 | 735.40 | 14/16 | 2 | $0.200 | 14/16 |
| grokbuild | 83987 | 15/15 | 635.68 | 13/15 | 2 | $0.344 | 7/15 |

### Strict headline C — core four, token composition (5 strict cells)

Same strict construction, restricted to the 5 cells where every core-4 harness has the complete input/output/cache split AND under-cap wall data; all columns computed on the same cells. Output is priced ~3× input at these vendors; cache reads ~0.1×.

| Harness | input_uncached/solve | output/solve | cache_read/solve | s/solve under-cap |
| --- | ---: | ---: | ---: | ---: |
| pi | 22551 | 20912 | 310016 | 466.33 |
| opencode | 31440 | 27439 | 621513 | 644.84 |
| claude | 15893 | 33241 | 525568 | 604.47 |
| codex | 17864 | 36018 | 196288 | 713.71 |

Cells (5): deepseek-v4-flash × cancel-async-tasks; glm-5.2 × feal-differential-cryptanalysis; glm-5.2 × llm-inference-batching-scheduler; kimi-k2.7-code × feal-differential-cryptanalysis; kimi-k2.7-code × llm-inference-batching-scheduler.
Composition read (descriptive): pi has the lowest output (the expensive class) and is the only harness with output < input; claude runs the leanest context but generates verbosely; codex is output-heaviest; opencode re-reads most (highest input and ~2× cache traffic). Note claude's s/solve here (604s) differs from Strict B (526s) because the cell set differs — B includes deepseek cells where claude is fast.

### Per-model strict tables — all five harnesses

Same strict rule applied within each model: cells solved by all five harnesses with complete tokens_fresh and under-cap wall data; per cell median over solving trials, then median over cells. Solve rates are intentionally omitted here (different denominator scope — see Layer 2). Descriptive medians over 1–2 cells; not statistically separated.

**deepseek-v4-flash (2 strict cells: cancel-async-tasks, llm-inference-batching-scheduler)** — widest spread in the dataset:

| Harness | tokens_fresh/solve | s/solve under-cap |
| --- | ---: | ---: |
| pi | 49712 | 270.30 |
| claude | 81812 | 383.34 |
| opencode | 83134 | 441.63 |
| codex | 109336 | 637.22 |
| grokbuild | 128112 | 720.13 |

**glm-5.2 (1 strict cell: feal-differential-cryptanalysis)** — single-cell, direction only:

| Harness | tokens_fresh/solve | s/solve under-cap |
| --- | ---: | ---: |
| claude | 23463 | 262.71 |
| opencode | 24826 | 156.24 |
| pi | 34951 | 248.86 |
| grokbuild | 36088 | 251.32 |
| codex | 54468 | 713.71 |

**kimi-k2.7-code (2 strict cells: feal-differential-cryptanalysis, llm-inference-batching-scheduler)** — near-parity cluster:

| Harness | tokens_fresh/solve | s/solve under-cap |
| --- | ---: | ---: |
| opencode | 63068 | 695.99 |
| claude | 63069 | 730.05 |
| grokbuild | 71448 | 733.13 |
| pi | 71750 | 563.28 |
| codex | 85517 | 594.10 |

Pattern: harness efficiency spread scales with model verbosity — deepseek (long reasoning chains) separates harnesses up to ~2.6×; kimi (terse) compresses them to near-parity.

## 2. Layer 2 capability — solve rates with Wilson 95% CIs

| Harness | Model | Solves/valid | Solve rate | Wilson 95% CI |
| --- | --- | --- | --- | --- |
| pi | deepseek-v4-flash | 7/9 | 77.8% | 45.3%–93.7% |
| pi | glm-5.2 | 11/11 | 100.0% | 74.1%–100.0% |
| pi | kimi-k2.7-code | 8/12 | 66.7% | 39.1%–86.2% |
| opencode | deepseek-v4-flash | 6/12 | 50.0% | 25.4%–74.6% |
| opencode | glm-5.2 | 7/12 | 58.3% | 32.0%–80.7% |
| opencode | kimi-k2.7-code | 8/12 | 66.7% | 39.1%–86.2% |
| claude | deepseek-v4-flash | 6/12 | 50.0% | 25.4%–74.6% |
| claude | glm-5.2 | 5/12 | 41.7% | 19.3%–68.0% |
| claude | kimi-k2.7-code | 7/12 | 58.3% | 32.0%–80.7% |
| codex | deepseek-v4-flash | 6/12 | 50.0% | 25.4%–74.6% |
| codex | glm-5.2 | 5/12 | 41.7% | 19.3%–68.0% |
| codex | kimi-k2.7-code | 7/12 | 58.3% | 32.0%–80.7% |
| grokbuild | deepseek-v4-flash | 5/12 | 41.7% | 19.3%–68.0% |
| grokbuild | glm-5.2 | 5/12 | 41.7% | 19.3%–68.0% |
| grokbuild | kimi-k2.7-code | 7/12 | 58.3% | 32.0%–80.7% |

All-run denominator sensitivity for pi: valid-only is primary per methodology, but all 4 infra exclusions are pi rows, each an exit-137 container OOM/environment kill documented in the methodology chain (the workdirs show infrastructure killed the runs, not agent surrender). If those infra rows are counted in denominators, pi/deepseek-v4-flash is 7/12 = 58.3%, and pi/glm-5.2 is 11/12 = 91.7%.
Infra row ids: pi:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial1, pi:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial2, pi:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial3, pi:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial2.

Per-cell n=3 intervals are too wide to rank; only pooled rows above are suitable for ranking, and only where CIs separate.

### Discriminator cells (per-harness conversion: solved/valid)
| Model | Task | pi | opencode | claude | codex | grokbuild |
| --- | --- | --- | --- | --- | --- | --- |
| deepseek-v4-flash | cancel-async-tasks | 2/3 | 1/3 | 1/3 | 3/3 | 2/3 |
| deepseek-v4-flash | feal-differential-cryptanalysis | 1/2 | 1/3 | 1/3 | 1/3 | 0/3 |
| deepseek-v4-flash | llm-inference-batching-scheduler | 3/3 | 3/3 | 3/3 | 2/3 | 2/3 |
| deepseek-v4-flash | schemelike-metacircular-eval | 1/1 | 1/3 | 1/3 | 0/3 | 1/3 |
| glm-5.2 | cancel-async-tasks | 3/3 | 0/3 | 0/3 | 1/3 | 0/3 |
| glm-5.2 | llm-inference-batching-scheduler | 3/3 | 3/3 | 1/3 | 1/3 | 1/3 |
| glm-5.2 | schemelike-metacircular-eval | 2/2 | 1/3 | 1/3 | 0/3 | 1/3 |
| kimi-k2.7-code | cancel-async-tasks | 1/3 | 0/3 | 1/3 | 0/3 | 0/3 |
| kimi-k2.7-code | feal-differential-cryptanalysis | 3/3 | 2/3 | 3/3 | 3/3 | 3/3 |
| kimi-k2.7-code | llm-inference-batching-scheduler | 3/3 | 3/3 | 2/3 | 3/3 | 3/3 |
| kimi-k2.7-code | schemelike-metacircular-eval | 1/3 | 3/3 | 1/3 | 1/3 | 1/3 |

## 3. Failure taxonomy per harness × model

| Harness | Model | Valid n | solved | wrong_answer | timeout | rate_limited | infra |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pi | deepseek-v4-flash | 9 | 7 | 1 | 1 | 0 | 3 |
| pi | glm-5.2 | 11 | 11 | 0 | 0 | 0 | 1 |
| pi | kimi-k2.7-code | 12 | 8 | 4 | 0 | 0 | 0 |
| opencode | deepseek-v4-flash | 12 | 6 | 6 | 0 | 0 | 0 |
| opencode | glm-5.2 | 12 | 7 | 3 | 2 | 0 | 0 |
| opencode | kimi-k2.7-code | 12 | 8 | 4 | 0 | 0 | 0 |
| claude | deepseek-v4-flash | 12 | 6 | 3 | 3 | 0 | 0 |
| claude | glm-5.2 | 12 | 5 | 3 | 4 | 0 | 0 |
| claude | kimi-k2.7-code | 12 | 7 | 2 | 3 | 0 | 0 |
| codex | deepseek-v4-flash | 12 | 6 | 2 | 4 | 0 | 0 |
| codex | glm-5.2 | 12 | 5 | 2 | 5 | 0 | 0 |
| codex | kimi-k2.7-code | 12 | 7 | 3 | 2 | 0 | 0 |
| grokbuild | deepseek-v4-flash | 12 | 5 | 1 | 6 | 0 | 0 |
| grokbuild | glm-5.2 | 12 | 5 | 4 | 3 | 0 | 0 |
| grokbuild | kimi-k2.7-code | 12 | 7 | 4 | 1 | 0 | 0 |

## 4. Dollars per solve and dated price table

| Model | Vendor | Input/cache-miss $/1M | Cache-read/cache-hit $/1M | Output $/1M | Access date | Source |
| --- | --- | --- | --- | --- | --- | --- |
| deepseek-v4-flash | DeepSeek | $0.140 | $0.003 | $0.280 | 2026-07-09 | https://api-docs.deepseek.com/quick_start/pricing |
| glm-5.2 | Z.AI | $1.400 | $0.260 | $4.400 | 2026-07-09 | https://docs.z.ai/guides/overview/pricing.md |
| kimi-k2.7-code | Moonshot/Kimi | $0.950 | $0.190 | $4.000 | 2026-07-09 | https://platform.moonshot.ai/docs/pricing/chat-k27-code.md |

Formula: $/solve = input_uncached×input_price + cache_read×cache_price + output×output_price, divided by 1,000,000. The matched-cell $/solve headline above uses the same median-over-solving-trials per cell, then median over matched cells. No unconfirmed price was guessed.

## 5. Methods and caveats

Harness versions executed in container:
| Harness | Executed in-container version | Stamp/caveat |
| --- | --- | --- |
| pi | 0.80.3 in both machines/images | host stamps also 0.80.3 |
| opencode | 1.17.13–1.17.15 across a mid-collection image rebuild | benchmark policy is always-latest; drift disclosed |
| claude | 2.1.203 | per-row host stamps vary; docker lane executes the in-container CLI |
| codex | 0.142.5 | host stamps also 0.142.5 |
| grokbuild | 0.2.91 in both machines/images, same build hash verified | 0.2.82 stamps on 6 rows are host-side stamping artifact; bug filed |

Runs used cap=1200s, n=3 trials per harness×model×task cell, and checker-after-kill semantics: a killed agent can still score success if the post-kill workdir passes. Failure classes are solved, wrong_answer, timeout, rate_limited, and infra; solve-rate denominators use valid rows only, excluding rate_limited and infra. Token accounting uses parity-backfilled tokens_fresh (uncached input + output), with cache_read kept separate. Speed is under-cap-only (<1176s); solved cap-riders are disclosed rather than corrected. Completed=True near-cap rejects are wrong_answer rather than timeout. Executed grok is uniform 0.2.91 (same build hash verified in both images); the 0.2.82 stamps on 6 rows are host-side stamping artifacts (bug filed). Turns-fix presence is additionally evidenced by adapter-mount semantics and per-row turn counts (6–77, never the pre-fix constant 1). Reasoning-effort parity is approximate across lanes; GLM-5.2 maps to Z.AI high per vendor guidance and DeepSeek chat has no effort knob beyond thinking on/off. Codex open-model lanes use a host-side LiteLLM Responses↔Chat bridge; transcript observability is asymmetric (Claude buffers output until exit; Codex JSONL lacks per-event timestamps).

Known lane caveats: DeepSeek's benchmark verbosity gap is attributed to harness prompting/agent style rather than endpoint asymmetry; opencode × deepseek × feal had two length-limit wrong_answer trials after burning 32k reasoning tokens with zero output; grokbuild crashes on Z.AI's nonstandard `finish_reason=network_error` are a robustness finding, not contamination; and the grok×kimi column is included only after the methodology's contamination-sweep gate (final JSONL has post-sweep grokbuild×kimi rows).

Sensitivity notes required by methodology: internal-timeout-touched rows are 7 total, all opencode, with 3 failures; rankings are unchanged if those rows/cells are excluded per the binding audit. OOM recovery details are listed below. Grokbuild scalar_exact token rows are on pre-fix-compatible columns; pooled per-harness medians are appendix-only because solve composition differs and can create survivorship bias.

## 6. Appendix A — pooled per-harness medians (solve-composition labeled; not headline)

| Harness | Model | Solves | Solve composition | pooled tokens_fresh | token cov | pooled under-cap s | speed cov | pooled $ | $ cov |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pi | deepseek-v4-flash | 7 | cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 69375 | 6/7 | 443.15 | 7/7 | $0.004 | 3/7 |
| pi | glm-5.2 | 11 | cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 58024 | 9/11 | 450.22 | 9/11 | $0.170 | 9/11 |
| pi | kimi-k2.7-code | 8 | cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 91840 | 8/8 | 623.47 | 6/8 | $0.339 | 8/8 |
| opencode | deepseek-v4-flash | 6 | cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 128726 | 6/6 | 811.47 | 6/6 | $0.038 | 6/6 |
| opencode | glm-5.2 | 7 | feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 73236 | 7/7 | 533.47 | 6/7 | $0.342 | 7/7 |
| opencode | kimi-k2.7-code | 8 | feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 73333 | 8/8 | 644.84 | 4/8 | $0.291 | 8/8 |
| claude | deepseek-v4-flash | 6 | cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 68679 | 5/6 | 394.99 | 5/6 | $0.019 | 5/6 |
| claude | glm-5.2 | 5 | feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 37407 | 4/5 | 415.89 | 4/5 | $0.142 | 4/5 |
| claude | kimi-k2.7-code | 7 | cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 42010 | 6/7 | 684.40 | 6/7 | $0.150 | 6/7 |
| codex | deepseek-v4-flash | 6 | cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler | 89188 | 6/6 | 614.50 | 6/6 | $0.023 | 6/6 |
| codex | glm-5.2 | 5 | cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler | 63420 | 5/5 | 888.45 | 5/5 | $0.295 | 5/5 |
| codex | kimi-k2.7-code | 7 | feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 114763 | 5/7 | 603.89 | 5/7 | $0.604 | 5/7 |
| grokbuild | deepseek-v4-flash | 5 | cancel-async-tasks, llm-inference-batching-scheduler, schemelike-metacircular-eval | 96455 | 5/5 | 771.71 | 5/5 | $n/a | 0/5 |
| grokbuild | glm-5.2 | 5 | feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 42225 | 5/5 | 250.96 | 3/5 | $n/a | 0/5 |
| grokbuild | kimi-k2.7-code | 7 | feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval | 78628 | 7/7 | 683.96 | 6/7 | $0.305 | 7/7 |

## 7. Appendix B — per-cell grid (task × model × harness medians)

| Model | Task | Harness | solves/valid | tok med | tok cov | s med | s cov | cap riders | $ med | $ cov | input med | cache med | output med |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| deepseek-v4-flash | cancel-async-tasks | claude | 1/3 | 53172 | 1/1 | 370.75 | 1/1 | 0 | $0.015 | 1/1 | 13010 | 525568 | 40162 |
| deepseek-v4-flash | cancel-async-tasks | codex | 3/3 | 32463 | 3/3 | 226.96 | 3/3 | 0 | $0.008 | 3/3 | 11391 | 88320 | 21072 |
| deepseek-v4-flash | cancel-async-tasks | grokbuild | 2/3 | 50145 | 2/2 | 417.04 | 2/2 | 0 | $n/a | 0/2 | n/a | n/a | n/a |
| deepseek-v4-flash | cancel-async-tasks | opencode | 1/3 | 11240 | 1/1 | 103.68 | 1/1 | 0 | $0.003 | 1/1 | 534 | 95232 | 10706 |
| deepseek-v4-flash | cancel-async-tasks | pi | 2/3 | 12157 | 2/2 | 95.96 | 2/2 | 0 | $0.003 | 2/2 | 1825 | 76544 | 10333 |
| deepseek-v4-flash | llm-inference-batching-scheduler | claude | 3/3 | 110451 | 3/3 | 394.99 | 3/3 | 0 | $0.026 | 3/3 | 65558 | 1529344 | 44893 |
| deepseek-v4-flash | llm-inference-batching-scheduler | codex | 2/3 | 186208 | 2/2 | 1047.74 | 2/2 | 0 | $0.046 | 2/2 | 80316 | 1797824 | 105892 |
| deepseek-v4-flash | llm-inference-batching-scheduler | grokbuild | 2/3 | 206080 | 2/2 | 1022.89 | 2/2 | 0 | $n/a | 0/2 | n/a | n/a | n/a |
| deepseek-v4-flash | llm-inference-batching-scheduler | opencode | 3/3 | 155028 | 3/3 | 781.21 | 3/3 | 0 | $0.047 | 3/3 | 68571 | 4692480 | 86457 |
| deepseek-v4-flash | llm-inference-batching-scheduler | pi | 3/3 | 87266 | 2/3 | 443.15 | 3/3 | 0 | $n/a | 0/3 | n/a | n/a | n/a |
| glm-5.2 | feal-differential-cryptanalysis | claude | 3/3 | 23463 | 3/3 | 262.81 | 3/3 | 0 | $0.081 | 3/3 | 12420 | 54464 | 11500 |
| glm-5.2 | feal-differential-cryptanalysis | codex | 3/3 | 54468 | 3/3 | 713.71 | 3/3 | 0 | $0.227 | 3/3 | 10871 | 99456 | 42137 |
| glm-5.2 | feal-differential-cryptanalysis | grokbuild | 3/3 | 36088 | 3/3 | 250.96 | 3/3 | 0 | $n/a | 0/3 | n/a | n/a | n/a |
| glm-5.2 | feal-differential-cryptanalysis | opencode | 3/3 | 24826 | 3/3 | 156.25 | 3/3 | 0 | $0.072 | 3/3 | 16510 | 81600 | 8316 |
| glm-5.2 | feal-differential-cryptanalysis | pi | 3/3 | 34951 | 3/3 | 248.90 | 3/3 | 0 | $0.098 | 3/3 | 22551 | 46656 | 12400 |
| glm-5.2 | llm-inference-batching-scheduler | claude | 1/3 | 114031 | 1/1 | 1099.47 | 1/1 | 0 | $0.476 | 1/1 | 65919 | 661696 | 48112 |
| glm-5.2 | llm-inference-batching-scheduler | codex | 1/3 | 99995 | 1/1 | 944.09 | 1/1 | 0 | $0.368 | 1/1 | 40913 | 196288 | 59082 |
| glm-5.2 | llm-inference-batching-scheduler | grokbuild | 1/3 | 186835 | 1/1 | n/a | 0/1 | 1 | $n/a | 0/1 | n/a | n/a | n/a |
| glm-5.2 | llm-inference-batching-scheduler | opencode | 3/3 | 92828 | 3/3 | 969.73 | 3/3 | 0 | $0.445 | 3/3 | 51154 | 730944 | 41674 |
| glm-5.2 | llm-inference-batching-scheduler | pi | 3/3 | 82905 | 3/3 | 710.89 | 3/3 | 0 | $0.460 | 3/3 | 46598 | 705024 | 34954 |
| kimi-k2.7-code | feal-differential-cryptanalysis | claude | 3/3 | 34611 | 3/3 | 604.47 | 3/3 | 0 | $0.135 | 3/3 | 15893 | 239616 | 18718 |
| kimi-k2.7-code | feal-differential-cryptanalysis | codex | 3/3 | 39997 | 2/3 | 430.73 | 2/3 | 1 | $0.174 | 2/3 | 17865 | 359424 | 22132 |
| kimi-k2.7-code | feal-differential-cryptanalysis | grokbuild | 3/3 | 58909 | 3/3 | 635.68 | 3/3 | 0 | $0.254 | 3/3 | 38232 | 685568 | 23470 |
| kimi-k2.7-code | feal-differential-cryptanalysis | opencode | 2/3 | 59599 | 2/2 | 747.14 | 2/2 | 0 | $0.267 | 2/2 | 31441 | 654503 | 28159 |
| kimi-k2.7-code | feal-differential-cryptanalysis | pi | 3/3 | 43211 | 3/3 | 466.33 | 3/3 | 0 | $0.164 | 3/3 | 22299 | 310016 | 20912 |
| kimi-k2.7-code | llm-inference-batching-scheduler | claude | 2/3 | 91528 | 2/2 | 855.81 | 2/2 | 0 | $0.334 | 2/2 | 58287 | 769280 | 33241 |
| kimi-k2.7-code | llm-inference-batching-scheduler | codex | 3/3 | 131038 | 3/3 | 757.09 | 3/3 | 0 | $0.694 | 3/3 | 96520 | 2028544 | 36018 |
| kimi-k2.7-code | llm-inference-batching-scheduler | grokbuild | 3/3 | 83987 | 3/3 | 831.13 | 3/3 | 0 | $0.344 | 3/3 | 50353 | 852992 | 33634 |
| kimi-k2.7-code | llm-inference-batching-scheduler | opencode | 3/3 | 66536 | 3/3 | 644.84 | 2/3 | 1 | $0.265 | 3/3 | 39097 | 621513 | 27439 |
| kimi-k2.7-code | llm-inference-batching-scheduler | pi | 3/3 | 100290 | 3/3 | 660.60 | 3/3 | 0 | $0.340 | 3/3 | 58846 | 897404 | 31349 |
| kimi-k2.7-code | schemelike-metacircular-eval | claude | 1/3 | n/a | 0/1 | n/a | 0/1 | 1 | $n/a | 0/1 | n/a | n/a | n/a |
| kimi-k2.7-code | schemelike-metacircular-eval | codex | 1/3 | n/a | 0/1 | n/a | 0/1 | 1 | $n/a | 0/1 | n/a | n/a | n/a |
| kimi-k2.7-code | schemelike-metacircular-eval | grokbuild | 1/3 | 103895 | 1/1 | n/a | 0/1 | 1 | $0.346 | 1/1 | 65581 | 687616 | 38314 |
| kimi-k2.7-code | schemelike-metacircular-eval | opencode | 3/3 | 93827 | 3/3 | n/a | 0/3 | 3 | $0.392 | 3/3 | 59756 | 985888 | 33768 |
| kimi-k2.7-code | schemelike-metacircular-eval | pi | 1/3 | 101524 | 1/1 | n/a | 0/1 | 1 | $0.708 | 1/1 | 60521 | 2562770 | 41003 |

## 8. Appendix C — tokens_fresh coverage and token_basis

| Harness | Model | solved tokens_fresh | all-row tokens_fresh | vendor_split | scalar_exact | unavailable |
| --- | --- | --- | --- | --- | --- | --- |
| pi | deepseek-v4-flash | 6/7 | 7/12 | 4 | 3 | 5 |
| pi | glm-5.2 | 9/11 | 9/12 | 9 | 0 | 3 |
| pi | kimi-k2.7-code | 8/8 | 12/12 | 12 | 0 | 0 |
| opencode | deepseek-v4-flash | 6/6 | 12/12 | 12 | 0 | 0 |
| opencode | glm-5.2 | 7/7 | 12/12 | 12 | 0 | 0 |
| opencode | kimi-k2.7-code | 8/8 | 12/12 | 12 | 0 | 0 |
| claude | deepseek-v4-flash | 5/6 | 8/12 | 8 | 0 | 4 |
| claude | glm-5.2 | 4/5 | 7/12 | 7 | 0 | 5 |
| claude | kimi-k2.7-code | 6/7 | 8/12 | 8 | 0 | 4 |
| codex | deepseek-v4-flash | 6/6 | 8/12 | 8 | 0 | 4 |
| codex | glm-5.2 | 5/5 | 7/12 | 7 | 0 | 5 |
| codex | kimi-k2.7-code | 5/7 | 8/12 | 8 | 0 | 4 |
| grokbuild | deepseek-v4-flash | 5/5 | 12/12 | 0 | 12 | 0 |
| grokbuild | glm-5.2 | 5/5 | 12/12 | 0 | 12 | 0 |
| grokbuild | kimi-k2.7-code | 7/7 | 12/12 | 11 | 1 | 0 |

Token-basis totals: {"scalar_exact": 28, "unavailable": 34, "vendor_split": 118}. Solved rows with null tokens_fresh: 8: claude:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial2, claude:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial1, claude:terminal-bench/schemelike-metacircular-eval:kimi-k2.7-code:trial1, codex:terminal-bench/feal-differential-cryptanalysis:kimi-k2.7-code:trial1, codex:terminal-bench/schemelike-metacircular-eval:kimi-k2.7-code:trial1, pi:terminal-bench/llm-inference-batching-scheduler:deepseek-v4-flash:trial3, pi:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial1, pi:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial3.
Solved cap riders by harness (wall_time_s ≥ 1176): {"claude": 3, "codex": 2, "grokbuild": 3, "opencode": 5, "pi": 4}.
Internal-timeout-touched run_ids: opencode:terminal-bench/cancel-async-tasks:kimi-k2.7-code:trial2, opencode:terminal-bench/cancel-async-tasks:kimi-k2.7-code:trial3, opencode:terminal-bench/feal-differential-cryptanalysis:kimi-k2.7-code:trial1, opencode:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial1, opencode:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial1, opencode:terminal-bench/schemelike-metacircular-eval:kimi-k2.7-code:trial1, opencode:terminal-bench/schemelike-metacircular-eval:kimi-k2.7-code:trial2.
OOM pre-fix exit-137 run_ids: codex:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial2, pi:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial3, pi:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial2.
OOM-recovered/rerun run_ids: codex:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial2, pi:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial3; still infra in final JSONL: pi:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial2.
Grok×kimi inclusion gate evidence: 12/12 final grokbuild×kimi rows are post-sweep host-stamped 0.2.91 rows; executed grok is uniform 0.2.91 per the version table above.

## Verification

`stats_check.py` in `/Users/matthewlam/dev/openbench-stats` recomputes every aggregate through two independent paths, asserts equality, and asserts this report body exactly matches the recomputed rendering. Intermediate values are not rounded; formatting occurs only at render time.

<!-- STATS_JSON_START
{
  "capability": [
    {
      "harness": "pi",
      "model": "deepseek-v4-flash",
      "rate": 0.777777777778,
      "solves": 7,
      "valid_n": 9,
      "wilson_hi": 0.936774892882,
      "wilson_lo": 0.452588969107
    },
    {
      "harness": "pi",
      "model": "glm-5.2",
      "rate": 1.0,
      "solves": 11,
      "valid_n": 11,
      "wilson_hi": 1.0,
      "wilson_lo": 0.741167033032
    },
    {
      "harness": "pi",
      "model": "kimi-k2.7-code",
      "rate": 0.666666666667,
      "solves": 8,
      "valid_n": 12,
      "wilson_hi": 0.861879908909,
      "wilson_lo": 0.390622088873
    },
    {
      "harness": "opencode",
      "model": "deepseek-v4-flash",
      "rate": 0.5,
      "solves": 6,
      "valid_n": 12,
      "wilson_hi": 0.746218402366,
      "wilson_lo": 0.253781597634
    },
    {
      "harness": "opencode",
      "model": "glm-5.2",
      "rate": 0.583333333333,
      "solves": 7,
      "valid_n": 12,
      "wilson_hi": 0.806739686341,
      "wilson_lo": 0.31951131255
    },
    {
      "harness": "opencode",
      "model": "kimi-k2.7-code",
      "rate": 0.666666666667,
      "solves": 8,
      "valid_n": 12,
      "wilson_hi": 0.861879908909,
      "wilson_lo": 0.390622088873
    },
    {
      "harness": "claude",
      "model": "deepseek-v4-flash",
      "rate": 0.5,
      "solves": 6,
      "valid_n": 12,
      "wilson_hi": 0.746218402366,
      "wilson_lo": 0.253781597634
    },
    {
      "harness": "claude",
      "model": "glm-5.2",
      "rate": 0.416666666667,
      "solves": 5,
      "valid_n": 12,
      "wilson_hi": 0.68048868745,
      "wilson_lo": 0.193260313659
    },
    {
      "harness": "claude",
      "model": "kimi-k2.7-code",
      "rate": 0.583333333333,
      "solves": 7,
      "valid_n": 12,
      "wilson_hi": 0.806739686341,
      "wilson_lo": 0.31951131255
    },
    {
      "harness": "codex",
      "model": "deepseek-v4-flash",
      "rate": 0.5,
      "solves": 6,
      "valid_n": 12,
      "wilson_hi": 0.746218402366,
      "wilson_lo": 0.253781597634
    },
    {
      "harness": "codex",
      "model": "glm-5.2",
      "rate": 0.416666666667,
      "solves": 5,
      "valid_n": 12,
      "wilson_hi": 0.68048868745,
      "wilson_lo": 0.193260313659
    },
    {
      "harness": "codex",
      "model": "kimi-k2.7-code",
      "rate": 0.583333333333,
      "solves": 7,
      "valid_n": 12,
      "wilson_hi": 0.806739686341,
      "wilson_lo": 0.31951131255
    },
    {
      "harness": "grokbuild",
      "model": "deepseek-v4-flash",
      "rate": 0.416666666667,
      "solves": 5,
      "valid_n": 12,
      "wilson_hi": 0.68048868745,
      "wilson_lo": 0.193260313659
    },
    {
      "harness": "grokbuild",
      "model": "glm-5.2",
      "rate": 0.416666666667,
      "solves": 5,
      "valid_n": 12,
      "wilson_hi": 0.68048868745,
      "wilson_lo": 0.193260313659
    },
    {
      "harness": "grokbuild",
      "model": "kimi-k2.7-code",
      "rate": 0.583333333333,
      "solves": 7,
      "valid_n": 12,
      "wilson_hi": 0.806739686341,
      "wilson_lo": 0.31951131255
    }
  ],
  "coverage": [
    {
      "all_rows_tokens_fresh": "7/12",
      "harness": "pi",
      "model": "deepseek-v4-flash",
      "scalar_exact": 3,
      "solved_tokens_fresh": "6/7",
      "unavailable": 5,
      "vendor_split": 4
    },
    {
      "all_rows_tokens_fresh": "9/12",
      "harness": "pi",
      "model": "glm-5.2",
      "scalar_exact": 0,
      "solved_tokens_fresh": "9/11",
      "unavailable": 3,
      "vendor_split": 9
    },
    {
      "all_rows_tokens_fresh": "12/12",
      "harness": "pi",
      "model": "kimi-k2.7-code",
      "scalar_exact": 0,
      "solved_tokens_fresh": "8/8",
      "unavailable": 0,
      "vendor_split": 12
    },
    {
      "all_rows_tokens_fresh": "12/12",
      "harness": "opencode",
      "model": "deepseek-v4-flash",
      "scalar_exact": 0,
      "solved_tokens_fresh": "6/6",
      "unavailable": 0,
      "vendor_split": 12
    },
    {
      "all_rows_tokens_fresh": "12/12",
      "harness": "opencode",
      "model": "glm-5.2",
      "scalar_exact": 0,
      "solved_tokens_fresh": "7/7",
      "unavailable": 0,
      "vendor_split": 12
    },
    {
      "all_rows_tokens_fresh": "12/12",
      "harness": "opencode",
      "model": "kimi-k2.7-code",
      "scalar_exact": 0,
      "solved_tokens_fresh": "8/8",
      "unavailable": 0,
      "vendor_split": 12
    },
    {
      "all_rows_tokens_fresh": "8/12",
      "harness": "claude",
      "model": "deepseek-v4-flash",
      "scalar_exact": 0,
      "solved_tokens_fresh": "5/6",
      "unavailable": 4,
      "vendor_split": 8
    },
    {
      "all_rows_tokens_fresh": "7/12",
      "harness": "claude",
      "model": "glm-5.2",
      "scalar_exact": 0,
      "solved_tokens_fresh": "4/5",
      "unavailable": 5,
      "vendor_split": 7
    },
    {
      "all_rows_tokens_fresh": "8/12",
      "harness": "claude",
      "model": "kimi-k2.7-code",
      "scalar_exact": 0,
      "solved_tokens_fresh": "6/7",
      "unavailable": 4,
      "vendor_split": 8
    },
    {
      "all_rows_tokens_fresh": "8/12",
      "harness": "codex",
      "model": "deepseek-v4-flash",
      "scalar_exact": 0,
      "solved_tokens_fresh": "6/6",
      "unavailable": 4,
      "vendor_split": 8
    },
    {
      "all_rows_tokens_fresh": "7/12",
      "harness": "codex",
      "model": "glm-5.2",
      "scalar_exact": 0,
      "solved_tokens_fresh": "5/5",
      "unavailable": 5,
      "vendor_split": 7
    },
    {
      "all_rows_tokens_fresh": "8/12",
      "harness": "codex",
      "model": "kimi-k2.7-code",
      "scalar_exact": 0,
      "solved_tokens_fresh": "5/7",
      "unavailable": 4,
      "vendor_split": 8
    },
    {
      "all_rows_tokens_fresh": "12/12",
      "harness": "grokbuild",
      "model": "deepseek-v4-flash",
      "scalar_exact": 12,
      "solved_tokens_fresh": "5/5",
      "unavailable": 0,
      "vendor_split": 0
    },
    {
      "all_rows_tokens_fresh": "12/12",
      "harness": "grokbuild",
      "model": "glm-5.2",
      "scalar_exact": 12,
      "solved_tokens_fresh": "5/5",
      "unavailable": 0,
      "vendor_split": 0
    },
    {
      "all_rows_tokens_fresh": "12/12",
      "harness": "grokbuild",
      "model": "kimi-k2.7-code",
      "scalar_exact": 1,
      "solved_tokens_fresh": "7/7",
      "unavailable": 0,
      "vendor_split": 11
    }
  ],
  "dataset": {
    "failure_totals": {
      "infra": 4,
      "solved": 100,
      "timeout": 34,
      "wrong_answer": 42
    },
    "grok_kimi_post_sweep_rows": 12,
    "grok_kimi_rows": 12,
    "harnesses": [
      "pi",
      "opencode",
      "claude",
      "codex",
      "grokbuild"
    ],
    "infra_run_ids": [
      "pi:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial1",
      "pi:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial2",
      "pi:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial3",
      "pi:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial2"
    ],
    "internal_timeout_touched_failures": 3,
    "internal_timeout_touched_run_ids": [
      "opencode:terminal-bench/cancel-async-tasks:kimi-k2.7-code:trial2",
      "opencode:terminal-bench/cancel-async-tasks:kimi-k2.7-code:trial3",
      "opencode:terminal-bench/feal-differential-cryptanalysis:kimi-k2.7-code:trial1",
      "opencode:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial1",
      "opencode:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial1",
      "opencode:terminal-bench/schemelike-metacircular-eval:kimi-k2.7-code:trial1",
      "opencode:terminal-bench/schemelike-metacircular-eval:kimi-k2.7-code:trial2"
    ],
    "models": [
      "deepseek-v4-flash",
      "glm-5.2",
      "kimi-k2.7-code"
    ],
    "oom_status": {
      "pre_oom_exit137": [
        "codex:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial2",
        "pi:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial3",
        "pi:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial2"
      ],
      "recovered_run_ids": [
        "codex:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial2",
        "pi:terminal-bench/feal-differential-cryptanalysis:deepseek-v4-flash:trial3"
      ],
      "still_infra_run_ids": [
        "pi:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial2"
      ]
    },
    "pi_all_run_sensitivity": {
      "deepseek-v4-flash": {
        "n": 12,
        "rate": 0.583333333333,
        "solves": 7
      },
      "glm-5.2": {
        "n": 12,
        "rate": 0.916666666667,
        "solves": 11
      }
    },
    "rows": 180,
    "solved_cap_riders_by_harness": {
      "claude": 3,
      "codex": 2,
      "grokbuild": 3,
      "opencode": 5,
      "pi": 4
    },
    "solved_null_token_run_ids": [
      "claude:terminal-bench/schemelike-metacircular-eval:deepseek-v4-flash:trial2",
      "claude:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial1",
      "claude:terminal-bench/schemelike-metacircular-eval:kimi-k2.7-code:trial1",
      "codex:terminal-bench/feal-differential-cryptanalysis:kimi-k2.7-code:trial1",
      "codex:terminal-bench/schemelike-metacircular-eval:kimi-k2.7-code:trial1",
      "pi:terminal-bench/llm-inference-batching-scheduler:deepseek-v4-flash:trial3",
      "pi:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial1",
      "pi:terminal-bench/schemelike-metacircular-eval:glm-5.2:trial3"
    ],
    "tasks": [
      "cancel-async-tasks",
      "feal-differential-cryptanalysis",
      "llm-inference-batching-scheduler",
      "schemelike-metacircular-eval"
    ],
    "token_basis_totals": {
      "scalar_exact": 28,
      "unavailable": 34,
      "vendor_split": 118
    },
    "valid_rows": 176
  },
  "discriminator": [
    {
      "claude": "1/3",
      "codex": "3/3",
      "grokbuild": "2/3",
      "model": "deepseek-v4-flash",
      "opencode": "1/3",
      "pi": "2/3",
      "task": "terminal-bench/cancel-async-tasks"
    },
    {
      "claude": "1/3",
      "codex": "1/3",
      "grokbuild": "0/3",
      "model": "deepseek-v4-flash",
      "opencode": "1/3",
      "pi": "1/2",
      "task": "terminal-bench/feal-differential-cryptanalysis"
    },
    {
      "claude": "3/3",
      "codex": "2/3",
      "grokbuild": "2/3",
      "model": "deepseek-v4-flash",
      "opencode": "3/3",
      "pi": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler"
    },
    {
      "claude": "1/3",
      "codex": "0/3",
      "grokbuild": "1/3",
      "model": "deepseek-v4-flash",
      "opencode": "1/3",
      "pi": "1/1",
      "task": "terminal-bench/schemelike-metacircular-eval"
    },
    {
      "claude": "0/3",
      "codex": "1/3",
      "grokbuild": "0/3",
      "model": "glm-5.2",
      "opencode": "0/3",
      "pi": "3/3",
      "task": "terminal-bench/cancel-async-tasks"
    },
    {
      "claude": "1/3",
      "codex": "1/3",
      "grokbuild": "1/3",
      "model": "glm-5.2",
      "opencode": "3/3",
      "pi": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler"
    },
    {
      "claude": "1/3",
      "codex": "0/3",
      "grokbuild": "1/3",
      "model": "glm-5.2",
      "opencode": "1/3",
      "pi": "2/2",
      "task": "terminal-bench/schemelike-metacircular-eval"
    },
    {
      "claude": "1/3",
      "codex": "0/3",
      "grokbuild": "0/3",
      "model": "kimi-k2.7-code",
      "opencode": "0/3",
      "pi": "1/3",
      "task": "terminal-bench/cancel-async-tasks"
    },
    {
      "claude": "3/3",
      "codex": "3/3",
      "grokbuild": "3/3",
      "model": "kimi-k2.7-code",
      "opencode": "2/3",
      "pi": "3/3",
      "task": "terminal-bench/feal-differential-cryptanalysis"
    },
    {
      "claude": "2/3",
      "codex": "3/3",
      "grokbuild": "3/3",
      "model": "kimi-k2.7-code",
      "opencode": "3/3",
      "pi": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler"
    },
    {
      "claude": "1/3",
      "codex": "1/3",
      "grokbuild": "1/3",
      "model": "kimi-k2.7-code",
      "opencode": "3/3",
      "pi": "1/3",
      "task": "terminal-bench/schemelike-metacircular-eval"
    }
  ],
  "matched_by_harness": {
    "claude": {
      "cache_read_per_solve": 593632.0,
      "cap_rider_solves": 1,
      "cells": 7,
      "dollar_coverage": "13/14",
      "input_uncached_per_solve": 37089.75,
      "output_per_solve": 36701.5,
      "seconds_per_solve_under_cap": 499.731,
      "speed_under_cap_coverage": "13/14",
      "token_coverage": "13/14",
      "tokens_fresh_per_solve": 72349.75,
      "usd_per_solve": "0.108478155"
    },
    "codex": {
      "cache_read_per_solve": 277856.0,
      "cap_rider_solves": 2,
      "cells": 7,
      "dollar_coverage": "14/16",
      "input_uncached_per_solve": 29388.75,
      "output_per_solve": 39077.5,
      "seconds_per_solve_under_cap": 735.3995,
      "speed_under_cap_coverage": "14/16",
      "token_coverage": "14/16",
      "tokens_fresh_per_solve": 77231.5,
      "usd_per_solve": "0.2001912975"
    },
    "grokbuild": {
      "cache_read_per_solve": 687616,
      "cap_rider_solves": 2,
      "cells": 7,
      "dollar_coverage": "7/15",
      "input_uncached_per_solve": 50353,
      "output_per_solve": 33634,
      "seconds_per_solve_under_cap": 635.678,
      "speed_under_cap_coverage": "13/15",
      "token_coverage": "15/15",
      "tokens_fresh_per_solve": 83987,
      "usd_per_solve": "0.34443983"
    },
    "opencode": {
      "cache_read_per_solve": 654502.5,
      "cap_rider_solves": 4,
      "cells": 7,
      "dollar_coverage": "18/18",
      "input_uncached_per_solve": 39097,
      "output_per_solve": 28158.5,
      "seconds_per_solve_under_cap": 695.99025,
      "speed_under_cap_coverage": "14/18",
      "token_coverage": "18/18",
      "tokens_fresh_per_solve": 66536,
      "usd_per_solve": "0.26498562"
    },
    "pi": {
      "cache_read_per_solve": 507520.0,
      "cap_rider_solves": 1,
      "cells": 7,
      "dollar_coverage": "15/18",
      "input_uncached_per_solve": 34574.5,
      "output_per_solve": 26130.5,
      "seconds_per_solve_under_cap": 454.741,
      "speed_under_cap_coverage": "17/18",
      "token_coverage": "17/18",
      "tokens_fresh_per_solve": 82905,
      "usd_per_solve": "0.251744925"
    }
  },
  "matched_cells": [
    {
      "label": "deepseek-v4-flash \u00d7 cancel-async-tasks",
      "model": "deepseek-v4-flash",
      "task": "terminal-bench/cancel-async-tasks"
    },
    {
      "label": "deepseek-v4-flash \u00d7 llm-inference-batching-scheduler",
      "model": "deepseek-v4-flash",
      "task": "terminal-bench/llm-inference-batching-scheduler"
    },
    {
      "label": "glm-5.2 \u00d7 feal-differential-cryptanalysis",
      "model": "glm-5.2",
      "task": "terminal-bench/feal-differential-cryptanalysis"
    },
    {
      "label": "glm-5.2 \u00d7 llm-inference-batching-scheduler",
      "model": "glm-5.2",
      "task": "terminal-bench/llm-inference-batching-scheduler"
    },
    {
      "label": "kimi-k2.7-code \u00d7 feal-differential-cryptanalysis",
      "model": "kimi-k2.7-code",
      "task": "terminal-bench/feal-differential-cryptanalysis"
    },
    {
      "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
      "model": "kimi-k2.7-code",
      "task": "terminal-bench/llm-inference-batching-scheduler"
    },
    {
      "label": "kimi-k2.7-code \u00d7 schemelike-metacircular-eval",
      "model": "kimi-k2.7-code",
      "task": "terminal-bench/schemelike-metacircular-eval"
    }
  ],
  "per_cell": [
    {
      "cache_read_median": 525568,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.0145383504",
      "dollar_cov": "1/1",
      "harness": "claude",
      "input_uncached_median": 13010,
      "model": "deepseek-v4-flash",
      "output_median": 40162,
      "seconds_under_cap_median": 370.748,
      "solves_valid": 1,
      "speed_cov": "1/1",
      "task": "terminal-bench/cancel-async-tasks",
      "tokens_cov": "1/1",
      "tokens_fresh_median": 53172,
      "valid_n": 3
    },
    {
      "cache_read_median": 88320,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.007742196",
      "dollar_cov": "3/3",
      "harness": "codex",
      "input_uncached_median": 11391,
      "model": "deepseek-v4-flash",
      "output_median": 21072,
      "seconds_under_cap_median": 226.96,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/cancel-async-tasks",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 32463,
      "valid_n": 3
    },
    {
      "cache_read_median": null,
      "cap_rider_solves": 0,
      "cost_usd_median": null,
      "dollar_cov": "0/2",
      "harness": "grokbuild",
      "input_uncached_median": null,
      "model": "deepseek-v4-flash",
      "output_median": null,
      "seconds_under_cap_median": 417.0435,
      "solves_valid": 2,
      "speed_cov": "2/2",
      "task": "terminal-bench/cancel-async-tasks",
      "tokens_cov": "2/2",
      "tokens_fresh_median": 50144.5,
      "valid_n": 3
    },
    {
      "cache_read_median": 95232,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.0033390896",
      "dollar_cov": "1/1",
      "harness": "opencode",
      "input_uncached_median": 534,
      "model": "deepseek-v4-flash",
      "output_median": 10706,
      "seconds_under_cap_median": 103.679,
      "solves_valid": 1,
      "speed_cov": "1/1",
      "task": "terminal-bench/cancel-async-tasks",
      "tokens_cov": "1/1",
      "tokens_fresh_median": 11240,
      "valid_n": 3
    },
    {
      "cache_read_median": 76544.0,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.0033628532",
      "dollar_cov": "2/2",
      "harness": "pi",
      "input_uncached_median": 1824.5,
      "model": "deepseek-v4-flash",
      "output_median": 10332.5,
      "seconds_under_cap_median": 95.955,
      "solves_valid": 2,
      "speed_cov": "2/2",
      "task": "terminal-bench/cancel-async-tasks",
      "tokens_cov": "2/2",
      "tokens_fresh_median": 12157.0,
      "valid_n": 3
    },
    {
      "cache_read_median": 1529344,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.0260303232",
      "dollar_cov": "3/3",
      "harness": "claude",
      "input_uncached_median": 65558,
      "model": "deepseek-v4-flash",
      "output_median": 44893,
      "seconds_under_cap_median": 394.994,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 110451,
      "valid_n": 3
    },
    {
      "cache_read_median": 1797824.0,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.0459279072",
      "dollar_cov": "2/2",
      "harness": "codex",
      "input_uncached_median": 80316.0,
      "model": "deepseek-v4-flash",
      "output_median": 105892.0,
      "seconds_under_cap_median": 1047.7445,
      "solves_valid": 2,
      "speed_cov": "2/2",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "2/2",
      "tokens_fresh_median": 186208.0,
      "valid_n": 3
    },
    {
      "cache_read_median": null,
      "cap_rider_solves": 0,
      "cost_usd_median": null,
      "dollar_cov": "0/2",
      "harness": "grokbuild",
      "input_uncached_median": null,
      "model": "deepseek-v4-flash",
      "output_median": null,
      "seconds_under_cap_median": 1022.8935,
      "solves_valid": 2,
      "speed_cov": "2/2",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "2/2",
      "tokens_fresh_median": 206080.0,
      "valid_n": 3
    },
    {
      "cache_read_median": 4692480,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.046946844",
      "dollar_cov": "3/3",
      "harness": "opencode",
      "input_uncached_median": 68571,
      "model": "deepseek-v4-flash",
      "output_median": 86457,
      "seconds_under_cap_median": 781.205,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 155028,
      "valid_n": 3
    },
    {
      "cache_read_median": null,
      "cap_rider_solves": 0,
      "cost_usd_median": null,
      "dollar_cov": "0/3",
      "harness": "pi",
      "input_uncached_median": null,
      "model": "deepseek-v4-flash",
      "output_median": null,
      "seconds_under_cap_median": 443.154,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "2/3",
      "tokens_fresh_median": 87266.0,
      "valid_n": 3
    },
    {
      "cache_read_median": 54464,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.08145892",
      "dollar_cov": "3/3",
      "harness": "claude",
      "input_uncached_median": 12420,
      "model": "glm-5.2",
      "output_median": 11500,
      "seconds_under_cap_median": 262.807,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 23463,
      "valid_n": 3
    },
    {
      "cache_read_median": 99456,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.22659276",
      "dollar_cov": "3/3",
      "harness": "codex",
      "input_uncached_median": 10871,
      "model": "glm-5.2",
      "output_median": 42137,
      "seconds_under_cap_median": 713.706,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 54468,
      "valid_n": 3
    },
    {
      "cache_read_median": null,
      "cap_rider_solves": 0,
      "cost_usd_median": null,
      "dollar_cov": "0/3",
      "harness": "grokbuild",
      "input_uncached_median": null,
      "model": "glm-5.2",
      "output_median": null,
      "seconds_under_cap_median": 250.963,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 36088,
      "valid_n": 3
    },
    {
      "cache_read_median": 81600,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.07180168",
      "dollar_cov": "3/3",
      "harness": "opencode",
      "input_uncached_median": 16510,
      "model": "glm-5.2",
      "output_median": 8316,
      "seconds_under_cap_median": 156.251,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 24826,
      "valid_n": 3
    },
    {
      "cache_read_median": 46656,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.09826196",
      "dollar_cov": "3/3",
      "harness": "pi",
      "input_uncached_median": 22551,
      "model": "glm-5.2",
      "output_median": 12400,
      "seconds_under_cap_median": 248.901,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 34951,
      "valid_n": 3
    },
    {
      "cache_read_median": 661696,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.47602036",
      "dollar_cov": "1/1",
      "harness": "claude",
      "input_uncached_median": 65919,
      "model": "glm-5.2",
      "output_median": 48112,
      "seconds_under_cap_median": 1099.472,
      "solves_valid": 1,
      "speed_cov": "1/1",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "1/1",
      "tokens_fresh_median": 114031,
      "valid_n": 3
    },
    {
      "cache_read_median": 196288,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.36827388",
      "dollar_cov": "1/1",
      "harness": "codex",
      "input_uncached_median": 40913,
      "model": "glm-5.2",
      "output_median": 59082,
      "seconds_under_cap_median": 944.086,
      "solves_valid": 1,
      "speed_cov": "1/1",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "1/1",
      "tokens_fresh_median": 99995,
      "valid_n": 3
    },
    {
      "cache_read_median": null,
      "cap_rider_solves": 1,
      "cost_usd_median": null,
      "dollar_cov": "0/1",
      "harness": "grokbuild",
      "input_uncached_median": null,
      "model": "glm-5.2",
      "output_median": null,
      "seconds_under_cap_median": null,
      "solves_valid": 1,
      "speed_cov": "0/1",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "1/1",
      "tokens_fresh_median": 186835,
      "valid_n": 3
    },
    {
      "cache_read_median": 730944,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.44502664",
      "dollar_cov": "3/3",
      "harness": "opencode",
      "input_uncached_median": 51154,
      "model": "glm-5.2",
      "output_median": 41674,
      "seconds_under_cap_median": 969.73,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 92828,
      "valid_n": 3
    },
    {
      "cache_read_median": 705024,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.46046064",
      "dollar_cov": "3/3",
      "harness": "pi",
      "input_uncached_median": 46598,
      "model": "glm-5.2",
      "output_median": 34954,
      "seconds_under_cap_median": 710.888,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 82905,
      "valid_n": 3
    },
    {
      "cache_read_median": 239616,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.13549739",
      "dollar_cov": "3/3",
      "harness": "claude",
      "input_uncached_median": 15893,
      "model": "kimi-k2.7-code",
      "output_median": 18718,
      "seconds_under_cap_median": 604.468,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 34611,
      "valid_n": 3
    },
    {
      "cache_read_median": 359424.0,
      "cap_rider_solves": 1,
      "cost_usd_median": "0.173789835",
      "dollar_cov": "2/3",
      "harness": "codex",
      "input_uncached_median": 17864.5,
      "model": "kimi-k2.7-code",
      "output_median": 22132.0,
      "seconds_under_cap_median": 430.7285,
      "solves_valid": 3,
      "speed_cov": "2/3",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "2/3",
      "tokens_fresh_median": 39996.5,
      "valid_n": 3
    },
    {
      "cache_read_median": 685568,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.25444216",
      "dollar_cov": "3/3",
      "harness": "grokbuild",
      "input_uncached_median": 38232,
      "model": "kimi-k2.7-code",
      "output_median": 23470,
      "seconds_under_cap_median": 635.678,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 58909,
      "valid_n": 3
    },
    {
      "cache_read_median": 654502.5,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.26685795",
      "dollar_cov": "2/2",
      "harness": "opencode",
      "input_uncached_median": 31440.5,
      "model": "kimi-k2.7-code",
      "output_median": 28158.5,
      "seconds_under_cap_median": 747.138,
      "solves_valid": 2,
      "speed_cov": "2/2",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "2/2",
      "tokens_fresh_median": 59599.0,
      "valid_n": 3
    },
    {
      "cache_read_median": 310016,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.16373509",
      "dollar_cov": "3/3",
      "harness": "pi",
      "input_uncached_median": 22299,
      "model": "kimi-k2.7-code",
      "output_median": 20912,
      "seconds_under_cap_median": 466.328,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/feal-differential-cryptanalysis",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 43211,
      "valid_n": 3
    },
    {
      "cache_read_median": 769280.0,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.334499375",
      "dollar_cov": "2/2",
      "harness": "claude",
      "input_uncached_median": 58286.5,
      "model": "kimi-k2.7-code",
      "output_median": 33241.0,
      "seconds_under_cap_median": 855.807,
      "solves_valid": 2,
      "speed_cov": "2/2",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "2/2",
      "tokens_fresh_median": 91527.5,
      "valid_n": 3
    },
    {
      "cache_read_median": 2028544,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.69414078",
      "dollar_cov": "3/3",
      "harness": "codex",
      "input_uncached_median": 96520,
      "model": "kimi-k2.7-code",
      "output_median": 36018,
      "seconds_under_cap_median": 757.093,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 131038,
      "valid_n": 3
    },
    {
      "cache_read_median": 852992,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.34443983",
      "dollar_cov": "3/3",
      "harness": "grokbuild",
      "input_uncached_median": 50353,
      "model": "kimi-k2.7-code",
      "output_median": 33634,
      "seconds_under_cap_median": 831.132,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 83987,
      "valid_n": 3
    },
    {
      "cache_read_median": 621513,
      "cap_rider_solves": 1,
      "cost_usd_median": "0.26498562",
      "dollar_cov": "3/3",
      "harness": "opencode",
      "input_uncached_median": 39097,
      "model": "kimi-k2.7-code",
      "output_median": 27439,
      "seconds_under_cap_median": 644.8425,
      "solves_valid": 3,
      "speed_cov": "2/3",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 66536,
      "valid_n": 3
    },
    {
      "cache_read_median": 897404,
      "cap_rider_solves": 0,
      "cost_usd_median": "0.33975476",
      "dollar_cov": "3/3",
      "harness": "pi",
      "input_uncached_median": 58846,
      "model": "kimi-k2.7-code",
      "output_median": 31349,
      "seconds_under_cap_median": 660.601,
      "solves_valid": 3,
      "speed_cov": "3/3",
      "task": "terminal-bench/llm-inference-batching-scheduler",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 100290,
      "valid_n": 3
    },
    {
      "cache_read_median": null,
      "cap_rider_solves": 1,
      "cost_usd_median": null,
      "dollar_cov": "0/1",
      "harness": "claude",
      "input_uncached_median": null,
      "model": "kimi-k2.7-code",
      "output_median": null,
      "seconds_under_cap_median": null,
      "solves_valid": 1,
      "speed_cov": "0/1",
      "task": "terminal-bench/schemelike-metacircular-eval",
      "tokens_cov": "0/1",
      "tokens_fresh_median": null,
      "valid_n": 3
    },
    {
      "cache_read_median": null,
      "cap_rider_solves": 1,
      "cost_usd_median": null,
      "dollar_cov": "0/1",
      "harness": "codex",
      "input_uncached_median": null,
      "model": "kimi-k2.7-code",
      "output_median": null,
      "seconds_under_cap_median": null,
      "solves_valid": 1,
      "speed_cov": "0/1",
      "task": "terminal-bench/schemelike-metacircular-eval",
      "tokens_cov": "0/1",
      "tokens_fresh_median": null,
      "valid_n": 3
    },
    {
      "cache_read_median": 687616,
      "cap_rider_solves": 1,
      "cost_usd_median": "0.34620499",
      "dollar_cov": "1/1",
      "harness": "grokbuild",
      "input_uncached_median": 65581,
      "model": "kimi-k2.7-code",
      "output_median": 38314,
      "seconds_under_cap_median": null,
      "solves_valid": 1,
      "speed_cov": "0/1",
      "task": "terminal-bench/schemelike-metacircular-eval",
      "tokens_cov": "1/1",
      "tokens_fresh_median": 103895,
      "valid_n": 3
    },
    {
      "cache_read_median": 985888,
      "cap_rider_solves": 3,
      "cost_usd_median": "0.39183587",
      "dollar_cov": "3/3",
      "harness": "opencode",
      "input_uncached_median": 59756,
      "model": "kimi-k2.7-code",
      "output_median": 33768,
      "seconds_under_cap_median": null,
      "solves_valid": 3,
      "speed_cov": "0/3",
      "task": "terminal-bench/schemelike-metacircular-eval",
      "tokens_cov": "3/3",
      "tokens_fresh_median": 93827,
      "valid_n": 3
    },
    {
      "cache_read_median": 2562770,
      "cap_rider_solves": 1,
      "cost_usd_median": "0.70843325",
      "dollar_cov": "1/1",
      "harness": "pi",
      "input_uncached_median": 60521,
      "model": "kimi-k2.7-code",
      "output_median": 41003,
      "seconds_under_cap_median": null,
      "solves_valid": 1,
      "speed_cov": "0/1",
      "task": "terminal-bench/schemelike-metacircular-eval",
      "tokens_cov": "1/1",
      "tokens_fresh_median": 101524,
      "valid_n": 3
    }
  ],
  "pooled": [
    {
      "dollar_cov": "3/7",
      "harness": "pi",
      "model": "deepseek-v4-flash",
      "pooled_seconds_under_cap_median": 443.154,
      "pooled_tokens_fresh_median": 69375.0,
      "pooled_usd_median": "0.0037902256",
      "solve_composition": "cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 7,
      "speed_cov": "7/7",
      "tokens_cov": "6/7"
    },
    {
      "dollar_cov": "9/11",
      "harness": "pi",
      "model": "glm-5.2",
      "pooled_seconds_under_cap_median": 450.216,
      "pooled_tokens_fresh_median": 58024,
      "pooled_usd_median": "0.16985416",
      "solve_composition": "cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 11,
      "speed_cov": "9/11",
      "tokens_cov": "9/11"
    },
    {
      "dollar_cov": "8/8",
      "harness": "pi",
      "model": "kimi-k2.7-code",
      "pooled_seconds_under_cap_median": 623.4675,
      "pooled_tokens_fresh_median": 91840.0,
      "pooled_usd_median": "0.338576755",
      "solve_composition": "cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 8,
      "speed_cov": "6/8",
      "tokens_cov": "8/8"
    },
    {
      "dollar_cov": "6/6",
      "harness": "opencode",
      "model": "deepseek-v4-flash",
      "pooled_seconds_under_cap_median": 811.47,
      "pooled_tokens_fresh_median": 128726.0,
      "pooled_usd_median": "0.0377031984",
      "solve_composition": "cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 6,
      "speed_cov": "6/6",
      "tokens_cov": "6/6"
    },
    {
      "dollar_cov": "7/7",
      "harness": "opencode",
      "model": "glm-5.2",
      "pooled_seconds_under_cap_median": 533.4745,
      "pooled_tokens_fresh_median": 73236,
      "pooled_usd_median": "0.34161272",
      "solve_composition": "feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 7,
      "speed_cov": "6/7",
      "tokens_cov": "7/7"
    },
    {
      "dollar_cov": "8/8",
      "harness": "opencode",
      "model": "kimi-k2.7-code",
      "pooled_seconds_under_cap_median": 644.8425,
      "pooled_tokens_fresh_median": 73332.5,
      "pooled_usd_median": "0.29065825",
      "solve_composition": "feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 8,
      "speed_cov": "4/8",
      "tokens_cov": "8/8"
    },
    {
      "dollar_cov": "5/6",
      "harness": "claude",
      "model": "deepseek-v4-flash",
      "pooled_seconds_under_cap_median": 394.994,
      "pooled_tokens_fresh_median": 68679,
      "pooled_usd_median": "0.0191227512",
      "solve_composition": "cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 6,
      "speed_cov": "5/6",
      "tokens_cov": "5/6"
    },
    {
      "dollar_cov": "4/5",
      "harness": "claude",
      "model": "glm-5.2",
      "pooled_seconds_under_cap_median": 415.8885,
      "pooled_tokens_fresh_median": 37406.5,
      "pooled_usd_median": "0.14226476",
      "solve_composition": "feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 5,
      "speed_cov": "4/5",
      "tokens_cov": "4/5"
    },
    {
      "dollar_cov": "6/7",
      "harness": "claude",
      "model": "kimi-k2.7-code",
      "pooled_seconds_under_cap_median": 684.3955,
      "pooled_tokens_fresh_median": 42010.0,
      "pooled_usd_median": "0.149974235",
      "solve_composition": "cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 7,
      "speed_cov": "6/7",
      "tokens_cov": "6/7"
    },
    {
      "dollar_cov": "6/6",
      "harness": "codex",
      "model": "deepseek-v4-flash",
      "pooled_seconds_under_cap_median": 614.5,
      "pooled_tokens_fresh_median": 89188.0,
      "pooled_usd_median": "0.0232409268",
      "solve_composition": "cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler",
      "solves": 6,
      "speed_cov": "6/6",
      "tokens_cov": "6/6"
    },
    {
      "dollar_cov": "5/5",
      "harness": "codex",
      "model": "glm-5.2",
      "pooled_seconds_under_cap_median": 888.448,
      "pooled_tokens_fresh_median": 63420,
      "pooled_usd_median": "0.29505708",
      "solve_composition": "cancel-async-tasks, feal-differential-cryptanalysis, llm-inference-batching-scheduler",
      "solves": 5,
      "speed_cov": "5/5",
      "tokens_cov": "5/5"
    },
    {
      "dollar_cov": "5/7",
      "harness": "codex",
      "model": "kimi-k2.7-code",
      "pooled_seconds_under_cap_median": 603.891,
      "pooled_tokens_fresh_median": 114763,
      "pooled_usd_median": "0.60430311",
      "solve_composition": "feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 7,
      "speed_cov": "5/7",
      "tokens_cov": "5/7"
    },
    {
      "dollar_cov": "0/5",
      "harness": "grokbuild",
      "model": "deepseek-v4-flash",
      "pooled_seconds_under_cap_median": 771.714,
      "pooled_tokens_fresh_median": 96455,
      "pooled_usd_median": null,
      "solve_composition": "cancel-async-tasks, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 5,
      "speed_cov": "5/5",
      "tokens_cov": "5/5"
    },
    {
      "dollar_cov": "0/5",
      "harness": "grokbuild",
      "model": "glm-5.2",
      "pooled_seconds_under_cap_median": 250.963,
      "pooled_tokens_fresh_median": 42225,
      "pooled_usd_median": null,
      "solve_composition": "feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 5,
      "speed_cov": "3/5",
      "tokens_cov": "5/5"
    },
    {
      "dollar_cov": "7/7",
      "harness": "grokbuild",
      "model": "kimi-k2.7-code",
      "pooled_seconds_under_cap_median": 683.9555,
      "pooled_tokens_fresh_median": 78628,
      "pooled_usd_median": "0.30523852",
      "solve_composition": "feal-differential-cryptanalysis, llm-inference-batching-scheduler, schemelike-metacircular-eval",
      "solves": 7,
      "speed_cov": "6/7",
      "tokens_cov": "7/7"
    }
  ],
  "strict_headlines": {
    "all5": {
      "base_solved_cells": [
        {
          "label": "deepseek-v4-flash \u00d7 cancel-async-tasks",
          "model": "deepseek-v4-flash",
          "task": "terminal-bench/cancel-async-tasks"
        },
        {
          "label": "deepseek-v4-flash \u00d7 llm-inference-batching-scheduler",
          "model": "deepseek-v4-flash",
          "task": "terminal-bench/llm-inference-batching-scheduler"
        },
        {
          "label": "glm-5.2 \u00d7 feal-differential-cryptanalysis",
          "model": "glm-5.2",
          "task": "terminal-bench/feal-differential-cryptanalysis"
        },
        {
          "label": "glm-5.2 \u00d7 llm-inference-batching-scheduler",
          "model": "glm-5.2",
          "task": "terminal-bench/llm-inference-batching-scheduler"
        },
        {
          "label": "kimi-k2.7-code \u00d7 feal-differential-cryptanalysis",
          "model": "kimi-k2.7-code",
          "task": "terminal-bench/feal-differential-cryptanalysis"
        },
        {
          "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
          "model": "kimi-k2.7-code",
          "task": "terminal-bench/llm-inference-batching-scheduler"
        },
        {
          "label": "kimi-k2.7-code \u00d7 schemelike-metacircular-eval",
          "model": "kimi-k2.7-code",
          "task": "terminal-bench/schemelike-metacircular-eval"
        }
      ],
      "harnesses": [
        "pi",
        "opencode",
        "claude",
        "codex",
        "grokbuild"
      ],
      "label": "all-5",
      "metrics": {
        "speed": {
          "by_harness": {
            "claude": {
              "coverage": "12/12",
              "median": 394.994
            },
            "codex": {
              "coverage": "13/14",
              "median": 713.706
            },
            "grokbuild": {
              "coverage": "13/13",
              "median": 635.678
            },
            "opencode": {
              "coverage": "11/12",
              "median": 644.8425
            },
            "pi": {
              "coverage": "14/14",
              "median": 443.154
            }
          },
          "cells": [
            {
              "label": "deepseek-v4-flash \u00d7 cancel-async-tasks",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/cancel-async-tasks"
            },
            {
              "label": "deepseek-v4-flash \u00d7 llm-inference-batching-scheduler",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            },
            {
              "label": "glm-5.2 \u00d7 feal-differential-cryptanalysis",
              "model": "glm-5.2",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "kimi-k2.7-code \u00d7 feal-differential-cryptanalysis",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            }
          ]
        },
        "tokens": {
          "by_harness": {
            "claude": {
              "coverage": "13/13",
              "median": 72349.75
            },
            "codex": {
              "coverage": "14/15",
              "median": 77231.5
            },
            "grokbuild": {
              "coverage": "14/14",
              "median": 71448.0
            },
            "opencode": {
              "coverage": "15/15",
              "median": 63067.5
            },
            "pi": {
              "coverage": "16/17",
              "median": 63058.0
            }
          },
          "cells": [
            {
              "label": "deepseek-v4-flash \u00d7 cancel-async-tasks",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/cancel-async-tasks"
            },
            {
              "label": "deepseek-v4-flash \u00d7 llm-inference-batching-scheduler",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            },
            {
              "label": "glm-5.2 \u00d7 feal-differential-cryptanalysis",
              "model": "glm-5.2",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "glm-5.2 \u00d7 llm-inference-batching-scheduler",
              "model": "glm-5.2",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            },
            {
              "label": "kimi-k2.7-code \u00d7 feal-differential-cryptanalysis",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            }
          ]
        },
        "usd": {
          "by_harness": {
            "claude": {
              "coverage": "5/5",
              "median": "0.2349983825"
            },
            "codex": {
              "coverage": "5/6",
              "median": "0.4339653075"
            },
            "grokbuild": {
              "coverage": "6/6",
              "median": "0.299440995"
            },
            "opencode": {
              "coverage": "5/5",
              "median": "0.265921785"
            },
            "pi": {
              "coverage": "6/6",
              "median": "0.251744925"
            }
          },
          "cells": [
            {
              "label": "kimi-k2.7-code \u00d7 feal-differential-cryptanalysis",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            }
          ]
        },
        "usd_complete": {
          "by_harness": {
            "claude": {
              "coverage": "2/2",
              "median": "0.334499375"
            },
            "codex": {
              "coverage": "3/3",
              "median": "0.69414078"
            },
            "grokbuild": {
              "coverage": "3/3",
              "median": "0.34443983"
            },
            "opencode": {
              "coverage": "3/3",
              "median": "0.26498562"
            },
            "pi": {
              "coverage": "3/3",
              "median": "0.33975476"
            }
          },
          "cells": [
            {
              "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            }
          ]
        }
      }
    },
    "core4": {
      "base_solved_cells": [
        {
          "label": "deepseek-v4-flash \u00d7 cancel-async-tasks",
          "model": "deepseek-v4-flash",
          "task": "terminal-bench/cancel-async-tasks"
        },
        {
          "label": "deepseek-v4-flash \u00d7 feal-differential-cryptanalysis",
          "model": "deepseek-v4-flash",
          "task": "terminal-bench/feal-differential-cryptanalysis"
        },
        {
          "label": "deepseek-v4-flash \u00d7 llm-inference-batching-scheduler",
          "model": "deepseek-v4-flash",
          "task": "terminal-bench/llm-inference-batching-scheduler"
        },
        {
          "label": "glm-5.2 \u00d7 feal-differential-cryptanalysis",
          "model": "glm-5.2",
          "task": "terminal-bench/feal-differential-cryptanalysis"
        },
        {
          "label": "glm-5.2 \u00d7 llm-inference-batching-scheduler",
          "model": "glm-5.2",
          "task": "terminal-bench/llm-inference-batching-scheduler"
        },
        {
          "label": "kimi-k2.7-code \u00d7 feal-differential-cryptanalysis",
          "model": "kimi-k2.7-code",
          "task": "terminal-bench/feal-differential-cryptanalysis"
        },
        {
          "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
          "model": "kimi-k2.7-code",
          "task": "terminal-bench/llm-inference-batching-scheduler"
        },
        {
          "label": "kimi-k2.7-code \u00d7 schemelike-metacircular-eval",
          "model": "kimi-k2.7-code",
          "task": "terminal-bench/schemelike-metacircular-eval"
        }
      ],
      "harnesses": [
        "pi",
        "opencode",
        "claude",
        "codex"
      ],
      "label": "core-4",
      "metrics": {
        "speed": {
          "by_harness": {
            "claude": {
              "coverage": "14/14",
              "median": 525.588
            },
            "codex": {
              "coverage": "15/16",
              "median": 757.093
            },
            "opencode": {
              "coverage": "15/16",
              "median": 747.138
            },
            "pi": {
              "coverage": "18/18",
              "median": 466.328
            }
          },
          "cells": [
            {
              "label": "deepseek-v4-flash \u00d7 cancel-async-tasks",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/cancel-async-tasks"
            },
            {
              "label": "deepseek-v4-flash \u00d7 feal-differential-cryptanalysis",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "deepseek-v4-flash \u00d7 llm-inference-batching-scheduler",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            },
            {
              "label": "glm-5.2 \u00d7 feal-differential-cryptanalysis",
              "model": "glm-5.2",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "glm-5.2 \u00d7 llm-inference-batching-scheduler",
              "model": "glm-5.2",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            },
            {
              "label": "kimi-k2.7-code \u00d7 feal-differential-cryptanalysis",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            }
          ]
        },
        "tokens": {
          "by_harness": {
            "claude": {
              "coverage": "14/14",
              "median": 68679
            },
            "codex": {
              "coverage": "15/16",
              "median": 99995
            },
            "opencode": {
              "coverage": "16/16",
              "median": 66536
            },
            "pi": {
              "coverage": "17/18",
              "median": 72883
            }
          },
          "cells": [
            {
              "label": "deepseek-v4-flash \u00d7 cancel-async-tasks",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/cancel-async-tasks"
            },
            {
              "label": "deepseek-v4-flash \u00d7 feal-differential-cryptanalysis",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "deepseek-v4-flash \u00d7 llm-inference-batching-scheduler",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            },
            {
              "label": "glm-5.2 \u00d7 feal-differential-cryptanalysis",
              "model": "glm-5.2",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "glm-5.2 \u00d7 llm-inference-batching-scheduler",
              "model": "glm-5.2",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            },
            {
              "label": "kimi-k2.7-code \u00d7 feal-differential-cryptanalysis",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            }
          ]
        },
        "usd": {
          "by_harness": {
            "claude": {
              "coverage": "10/10",
              "median": "0.13549739"
            },
            "codex": {
              "coverage": "12/13",
              "median": "0.22659276"
            },
            "opencode": {
              "coverage": "12/12",
              "median": "0.26498562"
            },
            "pi": {
              "coverage": "14/14",
              "median": "0.16373509"
            }
          },
          "cells": [
            {
              "label": "deepseek-v4-flash \u00d7 cancel-async-tasks",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/cancel-async-tasks"
            },
            {
              "label": "glm-5.2 \u00d7 feal-differential-cryptanalysis",
              "model": "glm-5.2",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "glm-5.2 \u00d7 llm-inference-batching-scheduler",
              "model": "glm-5.2",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            },
            {
              "label": "kimi-k2.7-code \u00d7 feal-differential-cryptanalysis",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            }
          ]
        },
        "usd_complete": {
          "by_harness": {
            "claude": {
              "coverage": "7/7",
              "median": "0.2079791475"
            },
            "codex": {
              "coverage": "10/10",
              "median": "0.29743332"
            },
            "opencode": {
              "coverage": "10/10",
              "median": "0.16839365"
            },
            "pi": {
              "coverage": "11/11",
              "median": "0.21900836"
            }
          },
          "cells": [
            {
              "label": "deepseek-v4-flash \u00d7 cancel-async-tasks",
              "model": "deepseek-v4-flash",
              "task": "terminal-bench/cancel-async-tasks"
            },
            {
              "label": "glm-5.2 \u00d7 feal-differential-cryptanalysis",
              "model": "glm-5.2",
              "task": "terminal-bench/feal-differential-cryptanalysis"
            },
            {
              "label": "glm-5.2 \u00d7 llm-inference-batching-scheduler",
              "model": "glm-5.2",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            },
            {
              "label": "kimi-k2.7-code \u00d7 llm-inference-batching-scheduler",
              "model": "kimi-k2.7-code",
              "task": "terminal-bench/llm-inference-batching-scheduler"
            }
          ]
        }
      }
    }
  },
  "taxonomy": [
    {
      "harness": "pi",
      "infra": 3,
      "model": "deepseek-v4-flash",
      "rate_limited": 0,
      "solved": 7,
      "timeout": 1,
      "valid_n": 9,
      "wrong_answer": 1
    },
    {
      "harness": "pi",
      "infra": 1,
      "model": "glm-5.2",
      "rate_limited": 0,
      "solved": 11,
      "timeout": 0,
      "valid_n": 11,
      "wrong_answer": 0
    },
    {
      "harness": "pi",
      "infra": 0,
      "model": "kimi-k2.7-code",
      "rate_limited": 0,
      "solved": 8,
      "timeout": 0,
      "valid_n": 12,
      "wrong_answer": 4
    },
    {
      "harness": "opencode",
      "infra": 0,
      "model": "deepseek-v4-flash",
      "rate_limited": 0,
      "solved": 6,
      "timeout": 0,
      "valid_n": 12,
      "wrong_answer": 6
    },
    {
      "harness": "opencode",
      "infra": 0,
      "model": "glm-5.2",
      "rate_limited": 0,
      "solved": 7,
      "timeout": 2,
      "valid_n": 12,
      "wrong_answer": 3
    },
    {
      "harness": "opencode",
      "infra": 0,
      "model": "kimi-k2.7-code",
      "rate_limited": 0,
      "solved": 8,
      "timeout": 0,
      "valid_n": 12,
      "wrong_answer": 4
    },
    {
      "harness": "claude",
      "infra": 0,
      "model": "deepseek-v4-flash",
      "rate_limited": 0,
      "solved": 6,
      "timeout": 3,
      "valid_n": 12,
      "wrong_answer": 3
    },
    {
      "harness": "claude",
      "infra": 0,
      "model": "glm-5.2",
      "rate_limited": 0,
      "solved": 5,
      "timeout": 4,
      "valid_n": 12,
      "wrong_answer": 3
    },
    {
      "harness": "claude",
      "infra": 0,
      "model": "kimi-k2.7-code",
      "rate_limited": 0,
      "solved": 7,
      "timeout": 3,
      "valid_n": 12,
      "wrong_answer": 2
    },
    {
      "harness": "codex",
      "infra": 0,
      "model": "deepseek-v4-flash",
      "rate_limited": 0,
      "solved": 6,
      "timeout": 4,
      "valid_n": 12,
      "wrong_answer": 2
    },
    {
      "harness": "codex",
      "infra": 0,
      "model": "glm-5.2",
      "rate_limited": 0,
      "solved": 5,
      "timeout": 5,
      "valid_n": 12,
      "wrong_answer": 2
    },
    {
      "harness": "codex",
      "infra": 0,
      "model": "kimi-k2.7-code",
      "rate_limited": 0,
      "solved": 7,
      "timeout": 2,
      "valid_n": 12,
      "wrong_answer": 3
    },
    {
      "harness": "grokbuild",
      "infra": 0,
      "model": "deepseek-v4-flash",
      "rate_limited": 0,
      "solved": 5,
      "timeout": 6,
      "valid_n": 12,
      "wrong_answer": 1
    },
    {
      "harness": "grokbuild",
      "infra": 0,
      "model": "glm-5.2",
      "rate_limited": 0,
      "solved": 5,
      "timeout": 3,
      "valid_n": 12,
      "wrong_answer": 4
    },
    {
      "harness": "grokbuild",
      "infra": 0,
      "model": "kimi-k2.7-code",
      "rate_limited": 0,
      "solved": 7,
      "timeout": 1,
      "valid_n": 12,
      "wrong_answer": 4
    }
  ]
}
STATS_JSON_END -->
