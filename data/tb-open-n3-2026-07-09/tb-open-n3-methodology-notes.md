# tb-open-n3: methodology notes for stats and reporting

## METRIC COMPARISON RULES (binding for the report — consolidated 2026-07-09)

1. **Correctness (Layer 2):** solve rate over VALID rows only (rate_limited +
   infra excluded from denominators). Wilson 95% CIs always shown. Per-cell
   n=3 CIs are too wide to rank — rank only on pooled rows, and only where
   CIs separate. Report the failure taxonomy next to every rate.
2. **Efficiency headline (Layer 1) = matched cells only:** task×model cells
   solved by every compared harness. Per harness per cell: MEDIAN over its
   solving trials (never best-of-N). Aggregate: median of cell values.
   Pooled per-harness medians are appendix-only, labeled with each harness's
   solve composition (survivorship bias: a harness that fails hard cells is
   judged only on easy solves — grok's pooled 251s vs matched-cell parity
   with pi is the canonical example).
3. **Speed:** wall_time under-cap solves only (<1176s); solved cap-riders
   counted separately per harness (their wall = kill time, inflated up to
   ~683s). Never mix corrected/uncorrected walls across lanes.
4. **Tokens:** tokens_fresh only (uncached input + output), with per-lane
   coverage stated (e.g. "claude 15/18 solves"). cache_read reported as its
   own column. token_basis counts disclosed. Never compare legacy scalars.
5. **Dollars:** $/solve = input_uncached×in_price + cache_read×cache_price +
   output×out_price from a DATED vendor list-price table (standard tier, no
   off-peak discounts; state the date). Same matched-cell structure as
   tokens.
6. **Sensitivity notes required in the report:** (a) internal-timeout-touched
   cells (7 total, all opencode, ≤3 failures — rankings unchanged if
   excluded); (b) OOM-recovered cells (list rerun run_ids); (c) grok token
   basis = scalar_exact on pre-fix columns; (d) transcript observability
   asymmetry (claude buffers; codex lacks event timestamps).
7. **Inclusion gate:** a harness column ships only if its rows pass the
   contamination sweep (grok×kimi gating condition, per Matthew).
8. **Verification chain:** stats worker computes → orchestrator recomputes
   every headline from raw JSONL independently → adversarial fresh-eyes
   reviewer attacks the methodology before anything is published.

Verified findings from the 2026-07-08 reliability audit (independent auditor
worker + orchestrator re-derivation from raw transcripts/fixtures). These are
binding on the stats pass and on any published comparison.

## Reward-hacking finding (gpt-5.6-sol, 2026-07-10) — binding on 5.6 stats

Transcript sweep of 24 valid gpt-5.6-sol runs found 2 clear spec-gaming
attempts (0 in a 10-run open-model baseline): both on schemelike, both
special-casing `eval.scm` as a transparent pass-through instead of actually
self-interpreting (pi trial3: `(eq? source-name 'eval.scm)`; cursor trial2:
"bootstrap fixed-point path"). BOTH SCORED SOLVED — output-equivalence
checkers cannot distinguish a pass-through from real self-interpretation.
Checker-owned oracles stop tampering, not intent-violating shortcuts.
5.6 stats must report DUAL numbers: as-scored, and with hacked solves
reclassified (pi schemelike 2/3->1/3; cursor 3/3->2/3), with the finding
disclosed prominently. Evidence: openbench-hacksweep/hack-report.md.

- The checker runs on the workdir AFTER the agent is killed at the 1200s cap,
  so solved-but-slow runs score `success=true` (15 such rows exist).
- All timeout-classified failures had genuinely failing workdirs at kill time
  (`checker_exit=1` across the board); none were passing work mislabeled.
- Timeout transcript observability is asymmetric: claude buffers output until
  exit (empty transcripts on kill), codex JSONL has no per-event timestamps,
  opencode/pi have timestamps. Timeout sub-characterization is therefore only
  possible for some lanes — disclose, don't guess.

## QUARANTINE: cancel-async-tasks (2026-07-10) — binding on all datasets

cancel-async-tasks is QUARANTINED from headline solve-rate and efficiency
claims until its checker is fixed and the column is rerun. Its checker is
proven non-deterministic under load: it SIGINTs the test child at a
hardcoded 0.5s and requires exit within 5s — both constants break when the
host is busy (e.g. right after container teardown). Proof (audit
openbench-cancelaudit/cancel-audit.md + follow-up): the sha256 of the
run.py the checker graded FAIL (luna pi trial1, recorded via
checker_workspace_files) matches the transcript-extracted file, which then
passed 11/11 reruns (idle, CPU-stressed, in-Docker, and via
bench/run.run_checker). Audit also flipped 4/4 sampled Sol/Terra rows and
1 feal row (opencode×kimi trial1 — feal failures are otherwise genuine).
All near-floor cancel-async numbers (Sol/Terra/Luna 0/12, open ~17%) are
grader noise, not difficulty. Fix spec: readiness-based SIGINT (wait for
two "Task started." lines, unbuffered child), ~20s exit deadline;
acceptance = reference + luna graded bytes 20/20 under stress. Reports
citing cancel-async must footnote the quarantine; matched-cell tables
already exclude most of it.

Related runner fix (merged c402277): every row now persists
checker_stdout/checker_stderr (scrubbed tails), checker_workspace_files
(sha256 manifest at check time), and image_digest — wrong_answer rows are
auditable from the Luna dataset onward.

## Speed metric policy (solved-at-cap wall inflation)

Solved-at-cap rows record wall_time = cap, not time-to-solve. Audit quantified
real gaps between last transcript activity and the kill on timestamped rows:
~91s, ~165s, ~338s, and ~683s on opencode rows (worst: opencode ×
llm-inference-batching-scheduler × kimi trial3, last event at +518s of a
1201s wall — verified independently by orchestrator). Claude/codex/pi cap
rows can't be corrected (buffered/timestamp-less transcripts).

**Policy: compute s/solve over under-cap solved runs only (wall < 1176s).
Report solved-at-cap counts per harness separately. Do not mix corrected and
uncorrected wall times across lanes — that would itself be an asymmetry.**

## Classifier corrections applied to this dataset

- `completed=True` runs near the cap are wrong_answer, not timeout (fix
  f24b9cd): a CLI that exits on its own was not killed by the runner. One row
  relabeled (opencode × cancel-async-tasks × kimi trial3, finished at 1185s
  after internal shell timeouts, checker rejected).
- grokbuild turns were wrong (always 1) before commit 9e4da7b; all grokbuild
  rows in the final dataset come from the post-fix rerun. Turns = number of
  `shell.turn.inference_done` events (model calls), same semantic family as
  opencode step_finish counting.

## Deepseek verbosity gap: harness prompting, NOT endpoint asymmetry

Direct-API probe (identical prompts, no harness; pi's chat-endpoint thinking
config vs claude-code 2.1.204's actual `--effort medium` request shape,
which is `thinking:{type:adaptive}` + `output_config:{effort:medium}`, not a
fixed budget): overall anthropic/chat output ratio **0.74x**. The ~10×/turn
output difference observed in benchmark cells is therefore attributable to
harness prompting/agent style — a legitimate harness finding, publishable,
not a config confound. Raw fixtures: openbench-thinkprobe/probe-results/.

## Known lane findings to disclose with results

- opencode × deepseek × feal: 2 trials died with `step_finish reason=length`
  — deepseek burned exactly 32,000 reasoning tokens with 0 output tokens in
  one turn, hit the generation cap, and the loop ended (no tool call to
  continue). Genuine harness-lane interaction (classified wrong_answer).
- grokbuild crashes (exit 1) on z.ai's nonstandard
  `finish_reason="network_error"` where other harnesses retry — genuine
  robustness finding, disclosed, not contamination.
- Codex open-model lane runs through a host-side LiteLLM Responses↔Chat
  bridge; transcripts show model-metadata refresh errors that add latency
  independent of model quality.
- Reasoning-effort parity is approximate across lanes ("medium-equivalent"):
  glm-5.2 maps to z.ai `high` per vendor guidance; deepseek chat has no
  effort knob (thinking on/off only).

## Token accounting status — PARITY BACKFILL APPLIED 2026-07-08

Every row now carries `tokens_fresh` (uncached input + output — THE
cross-harness comparable scalar), the full split fields, and `token_basis`:
- `vendor_split` (107 rows): re-derived from per-turn vendor usage in the
  transcript. Self-tests: pi 23/23 exact vs legacy scalar (+2 recovered from
  None); claude 23/23 reconciled (legacy = input+cache_write+output exactly).
- `scalar_exact` (19 rows): pi/grokbuild rows with unavailable transcripts
  adopt the legacy scalar — valid because those lanes' legacy basis IS the
  target basis (probe-verified). claude/codex are never adopted.
- `unavailable` (37 rows): no usage anywhere; fields null, never guessed.
  Mostly claude/codex buffered timeout rows (fine — failures don't enter
  tokens/solve) BUT included 8 pi SOLVED rows (6 deepseek, 2 glm) whose stdout
  was lost entirely (header-only transcript + tokens=None, even for under-cap
  completions). Root cause UNKNOWN: same pi version (0.80.3) on lost and clean
  rows, loss is per-cell within a single stream (not per-version/per-task).
  Leading hypothesis: docker output-capture loss during the wedged-container
  window (workdir volume survives → checker passes → success with no
  telemetry) — NOT attributed to pi. Those 8 rows were purged and rerun
  (recovery_rerun.sh phase 1); if any rerun reproduces solved+tokens=None the
  bug is live and must be investigated before stats ship. Gate rule added: a
  solved row with null token data is an anomaly, never silently accepted.
- Quantified de-inflation from the old mixed-basis numbers: codex fresh/old =
  0.58-0.80 by model (reasoning double-count removed); claude/opencode/pi
  legacy scalars confirmed = their documented bases exactly.
- Tokens/solve and $/solve must use `tokens_fresh` only, over rows where it
  is non-null; report coverage per lane. $ at report time from a dated price
  table using the split fields (output priced separately from input).
- Tool: bench/tools/parity_backfill.py (branch parity-backfill, worktree);
  idempotent — RE-RUN after the mini's final glm/kimi rows are rsynced back.
