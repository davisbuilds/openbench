# OpenBench: same model, different harness — how much does the harness matter?

*OpenBench release analysis, 2026-07. Data: 2026-07-16 → 07-20, 15 tasks (8 core + 7 Terminal-Bench imports), n=3 trials/cell, deterministic checkers, hack-adjusted, counting-proxy metering where the protocol allows. Rows sorted by correctness, ties broken by efficiency.*

## Headline

Across four models and seven harnesses (~1,100 cells): **model choice sets the cost tier (15–20× spread); harness choice moves correctness by 10–30 points and efficiency by 2–5× within it — and the best harness depends on the model.**

## deepseek-v4-flash × 5 (final; 1200s cap; efficiency = all-solved intersection n=20, proxy-metered)

| arm | correct | med wall | in/solve | out | cache-read | $/solve |
|---|---|---|---|---|---|---|
| pi | 85% (29/34)* raw 64% | 15s | 8.5k | 2.9k | 49k | 2.9¢ |
| claude | 72% (31/43) | 20s | 11.4k | 4.5k | 117k | 3.1¢ |
| opencode | 64% (28/44) | 17s | 14.7k | 3.4k | 121k | 2.5¢ |
| grokbuild | 60% (27/45) | 23s | 9.0k | 3.4k | 201k | 3.9¢ |
| codex† | 59% (24/41) | 21s | 14.1k | 6.6k | 257k | 3.6¢ |

## kimi-k3 × 3 (final; 1200s cap; matched cells n=39; "finished" = timeouts excluded)

| arm | @cap | med wall | in/solve | out | cache-read | $/solve |
|---|---|---|---|---|---|---|
| grokbuild | 74% (29/39) | 56s | 15.4k | 3.7k | 92k | 46¢ |
| opencode | 69% (27/39) | 79s | 17.7k | 3.6k | 104k | 62¢ |
| pi | 64% (25/39) | 68s | 9.2k | 2.8k | 48k | 46¢ |

13% of kimi cells hit the 1200s cap (vs 4% deepseek, ~0% gpt-5.6@2400s); K3 is 3–4× slower per identical solved cell, so @cap rates conflate accuracy with speed. A planned 2400s rescue rerun could not be executed (Moonshot balance exhausted), so we make no timeout-adjusted accuracy claim; rankings are stable across the @cap and finished bases.

## grok-4.5 × 4 (final; subscription lanes; cursor/opencode CLI-reported tokens)

| arm | correct | med wall | in/solve | out | cache-read |
|---|---|---|---|---|---|
| cursor‡ | **91%** (41/45) | 36s | 29k | 9k | 272k |
| pi | 79% (34/43) | 35s | 26k | 8k | 139k |
| opencode | 79% (30/38) | 21s | 44k | 9k | 224k |
| grokbuild | 89% (40/45) | 23s | 35k | 6k | 139k |

## gpt-5.6-sol × 7 (final; 2400s cap, effectively timeout-free)

| arm | correct | med wall | in/solve | out | cache-read | basis |
|---|---|---|---|---|---|---|
| cursor‡ | **86%** (37/43) | 43s | — | 6k | 440k | cli |
| grokbuild | 82% (37/45) | 89s | 27k | 4k | 244k | proxy |
| opencode | 80% (36/45) | 61s | 37k | 6k | 320k | cli |
| devin | 80% (36/45) | 100s | — | — | — | estimated† |
| claude | 78% (35/45) | 61s | 33k | 6k | 155k | proxy |
| pi | 73% (32/44) | 40s | 33k | 5k | 111k | proxy |
| codex | 73% (32/44) | 96s | 89k | 13k | 1.06M | proxy |

## Harness × model (correctness)

| harness | deepseek | kimi-k3 | grok-4.5 | gpt-5.6 |
|---|---|---|---|---|
| cursor | n/a | n/a | **91%** | **86%** |
| devin | n/a | n/a | (running) | 80% |
| pi | **76%** | 64% | 79% | 73% |
| claude | 72% | — | n/a | 78% |
| opencode | 64% | 69% | 79% | 80% |
| grokbuild | 60% | **74%** | 89% | 82% |
| codex | 59% | — | n/a | 73% |

## Findings

1. **Cursor tops both frontier models it can run** (91%, 86%), zero timeouts/infra — with the caveat that it serves models through its own hosted deployments (‡) and blocks BYO models in its CLI, so it never competes on open models.
2. **Harness ranking is model-dependent**: grokbuild is kimi's best (74%) yet deepseek's near-worst (60%); opencode climbs from 64% (deepseek) to 80% (gpt-5.6); codex jumps 59%→73% moving to its home model. A single-model harness benchmark would mislead.
3. **pi is the most token-frugal harness where metered** — lowest cache-read on deepseek (49k/solve vs 117–257k) and kimi (48k vs 92–104k), lowest uncached input on kimi, lowest cache on gpt-5.6 among proxy-metered arms — but not universally dominant (grokbuild beats it outright on kimi; opencode is faster on grok-4.5). The cleanest controlled contrast: pi vs codex at tied 73% on gpt-5.6 — 2.4× faster, ~⅓ the input tokens, ~⅑ the cache traffic.
4. **Cost tiers dwarf harness spread**: deepseek ≈ 3¢/solve, kimi ≈ 50¢/solve, frontier = subscription. Pick the model for the budget, the harness for correctness+latency within it.
5. **grok-4.5 is the strongest model tested** on shared harnesses (beats gpt-5.6 on 3 of 4; opencode ties), and its home-harness advantage is small (grokbuild 89% vs cursor 91%) — unlike codex's +14-point jump on its home model.
6. **A controlled n=5 probe** (pi × gpt-5.5 vs gpt-5.6 on the three hardest TB tasks) shows pi's apparent gpt-5.6 regression was mostly variance: feal 5/5 vs 3/5 (small real gap); extract-elf and raman 0/5 on *both* models — task-hard, not model-regressed.
7. **Timeout caps entangle accuracy with speed for slow models**: 13% of kimi-k3 cells died at the 1200s cap vs ~0% for fast models; the pending 2400s rescue rerun measures how much correctness that cap costs.

## Method / honesty box

- Denominators: countable cells (infra/rate-limited excluded, counts reported); kimi uses matched cells across arms; efficiency uses all-solved intersections where final.
- *pi × deepseek: single source = the metered rerun (29/45 raw = 64%; 29/34 countable = 85% after 8 rate-limited + 3 infra exclusions from a DeepSeek 429 burst); an independent earlier run replicates at 32/45 raw = 71% (76% countable, no token data). All pi numbers in this table come from the one metered experiment. †codex × deepseek runs through a Responses→Chat bridge; some gap may be translation friction.
- deepseek/kimi arms ran with mixed host/container CLI versions (flagged per arm); grok-4.5/gpt-5.6 are fully version-aligned. Six reliability gates now enforce alignment + silent-failure detection (each maps to an incident this week, including a classifier over-match that briefly excluded 10 worked probe cells — crash markers are now gated on absence of model work).
- Prices: deepseek-v4-flash $0.14/M uncached-in, $0.0028/M cached, $0.28/M out; kimi-k3 $3/M in, $0.30/M cached, $15/M out (official docs, 2026-07; shipped as prices.json with the data).
- Sampling: harness defaults, deliberately not clamped (they're part of the product); observed sampling recorded per request where the wire allows.
- cursor/devin protocols block proxy metering → CLI-reported tokens (‡/cli basis), not cross-comparable with proxy rows. †devin's export reports cumulative uncached prompt tokens with no cache split, so its figure is a cache-equivalent ESTIMATE (last-step prompt + completions) synthesized by our adapter — ranked on correctness, excluded from token-efficiency comparisons.
- devin runs `--permission-mode dangerous` for execution parity (all harnesses auto-run commands) in disposable workdirs with an isolated HOME. The isolation exists because devin's CLI discovers the invoking user's global agent config: the first devin arm was contaminated by the operator's personal skills (plan-approval stops, review-subagent hangs) and was discarded and rerun clean — worth knowing if you run OpenBench on your own machine. opencode subscription lanes (gpt-5.6, grok-4.5) are also unproxied, but relay the vendor's own usage block (cli-relayed vendor usage) — unaudited totals, standard vendor split semantics. grokbuild rows are fully proxy-measured (label lived in token_basis_proxy).
- Not covered: long-horizon tasks, multi-agent modes (verified no-op in `codex exec`), cursor×open-models (CLI BYOK unsupported as of 2026.07.09).

## Efficiency on identical solved work (all-solved intersection)

Cells every arm solved — same tasks, same trials, so totals are directly comparable (total = uncached-in + cache-read + cache-write + output; cache accounting still per-vendor).

**gpt-5.6 (n=28; devin excluded — estimated basis, see honesty box):**

| arm | med wall | med total tokens | mean | basis |
|---|---|---|---|---|
| pi | 37s | **35k** | 109k | proxy |
| claude | 57s | 49k | 137k | proxy |
| opencode | 57s | 90k | 221k | cli |
| grokbuild | 62s | 142k | 325k | proxy |
| codex | 100s | 157k | 397k | proxy (est.) |
| cursor | **33s** | 186k | 380k | cli |

**grok-4.5 (n=29):**

| arm | med wall | med total tokens | mean | basis |
|---|---|---|---|---|
| pi | **16s** | **29k** | 111k | proxy |
| opencode | 17s | 71k | 149k | cli* |
| grokbuild | 31s | 73k | 195k | proxy |
| cursor | 18s | 86k | 205k | cli |

Correctness leaders are token-heavy: cursor solves the most but spends 4–6× pi's tokens per identical solve; pi is cheapest-per-solve on both frontier models while placing last or mid on correctness. The correctness-vs-efficiency frontier, not a single ranking, is the story.

## TODO before posting
- [x] all data final (grok-4.5 40/45; kimi rescue cancelled — balance exhausted; probe done)
- [ ] gpt-5.6/grok-4.5 all-solved intersection efficiency tables
- [x] opencode gpt-5.6 basis resolved: cli-relayed vendor usage (no proxy fields); grokbuild = proxy_measured under token_basis_proxy
- [ ] Matthew review (release pages built locally: gpt56 live; grok45/kimi/deepseek awaiting push)
