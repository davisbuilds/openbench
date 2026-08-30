# am-consistency-pr80 — full 6-arm Pareto ladder (2026-08-29)

Task: `tasks/am-consistency-pr80` (K=3, effects-only). A graded, long-context
bug-fix task built from real Codex PR-review findings on davisbuilds/agentmonitor
PR #80 — three cross-site-consistency defects planted in the 94-file `src/`
haystack, symptoms only, scored `fixed/3`. Native codex, serial, 3 trials/arm,
`exec_mode=local`. This run extends the terra/luna sanity pass
(`data/am-consistency-2026-08-29/`) with four OpenRouter open arms to place each
model on the score×cost frontier.

## The ladder (per trial)

Costs are **theoretical list-price** (derived by `experiments/analyze_cost.py`
from the vendor token split × the OpenRouter/OpenAI `/api/v1/models` price sheet)
— NOT authoritatively captured. See the cost-capture caveat below. Ranking is
robust to this: every arm is priced off the same list, codex included.

| arm | n valid | mean score | $/trial (derived) | t_agent | cache-reads/trial |
|---|---|---|---|---|---|
| gpt-5.6-terra-xhigh | 3 | **1.000** | $0.697 | 354s | 1.2M |
| gpt-5.6-luna-max | 3 | **1.000** | $0.274 | 851s | 8.1M |
| deepseek-v4-flash-0731 | 3 | 0.778 | **$0.075** | 873s | 3.1M |
| nemotron-3-ultra | 2 | 0.500 | ~$0.474 | 909s | 4.2M |
| minimax-m3 | 3 | 0.444 | $0.512 | 558s | 7.6M |
| glm-5.3-flash | 3 | 0.222 | $0.023 | 503s | 1.0M |

Per-trial scores: terra `[1,1,1]`, luna `[1,1,1]`, deepseek `[1.0, 0.33, 1.0]`,
nemotron `[0.33, 0.67]` (trial 3 excluded — rate_limited/incomplete),
minimax `[0.0, 0.67, 0.67]` (trial 1 was a **no-op**: returned an answer without
editing any file, `workspace_changed=False` — a reliability failure, not a
wrong-code attempt; edited-attempts-only mean is 0.67), glm `[0.33, 0.0, 0.33]`.

## Pareto frontier (maximize score, minimize $/trial)

Non-dominated set: **glm-5.3-flash, deepseek-v4-flash-0731, gpt-5.6-luna-max.**

- **deepseek-v4-flash-0731 is the value pick** for this bug-fix class: 0.78 mean
  accuracy at **$0.075/trial** — ~9× cheaper than terra, ~3.6× cheaper than luna,
  at 78% of frontier accuracy. Best score-per-dollar of any arm that actually
  engages the task. A legitimate daily driver for cross-site-consistency fixes
  you'll review anyway.
- **luna-max dominates terra-xhigh on the score×cost plane.** Both score a clean
  1.000; luna is **$0.274 vs terra $0.697/trial**. terra is therefore OFF the
  score×cost frontier — its only justification is **latency**: 354s vs luna's
  851s (2.4×). Routing rule: need it right + cheap → luna; need it right + fast
  → terra. (This nuances the earlier "luna thrashes long-context" finding: luna
  DOES re-read the codebase 6.7× more (8.1M vs 1.2M cache-reads), but cache-reads
  are cheap and luna's per-token price is ~4× below terra's xhigh tier, so luna
  still lands cheaper overall.)
- **minimax-m3 and nemotron-3-ultra are dominated — skip them for this class.**
  deepseek beats both on BOTH axes (higher score, lower cost). minimax pairs low
  accuracy (0.44) with high cost ($0.51, from 7.6M cache-read thrash at a higher
  per-token rate than glm). nemotron adds an operational strike: it rate-limited
  and stalled badly (one trial ran 71+ min; trial 3 never completed), so it is
  unreliable here on top of being dominated.
- **glm-5.3-flash** is rock-bottom cost ($0.023) but only 0.22 accuracy — fine
  for trivial edits, not real cross-site-consistency fixes.

## Headline

The defect class is *within* both frontier arms (terra/luna both 1.0) — the open
field is not. On this real long-context bug-fix work: **deepseek-v4-flash-0731**
is the value sweet spot, **luna-max** is the cheap-but-slow way to a guaranteed
fix, **terra-xhigh** buys speed at a premium, and **minimax / nemotron / glm are
not worth routing here**.

## Deferred

- **kimi-k3** (Moonshot, $3/$15 per M — frontier-priced): routing is wired and
  validated (`~/.openbench/open_models.toml` override + `bridge/config.yaml`), but
  the run is **deferred pending OpenRouter credits**. Recorded as UNRESOLVED, not
  assumed to tie the frontier — the base rate for open arms matching the 1.0
  frontier on this task is 0 of 4. One paid run when credits allow would settle
  it. Kept in the spec, unrun.

## Caveats

- **Cost was NOT authoritatively captured.** All open arms returned
  `cost_usd=None` / `cost_source=None` despite the bridge sending
  `extra_body.usage.include` — the capture path did not land (see
  `docs/project/BACKLOG.md` → "Cost telemetry"). Costs here are price-sheet
  theoretical. Because `results.jsonl` keeps only the final row per cell,
  throttled/retried attempts are uncounted, so these are a **floor** on real
  spend.
- **n=2 for nemotron** (trial 3 excluded, rate_limited); its per-trial cost
  includes partial trial-3 tokens, so treat it as approximate.
- Codex-native arms (terra/luna) run on the ChatGPT subscription, so their cost
  is list-price theoretical by construction (they never touch a metered endpoint).
- Local exec; the checker provisions deps by symlinking a canonical agentmonitor
  `node_modules` read-only. Machine-specific, fork-local.
