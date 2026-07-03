# Same model, different wrapper: does the coding agent's harness matter?

A coding agent is two things bolted together: a **model** that predicts tokens,
and a **harness** — the CLI around it that manages the run loop, the tool calls,
the permission prompts, the context it feeds back in. We talk endlessly about the
model. We rarely measure the harness. OpenBench is a small, from-scratch benchmark
that asks one question and tries to answer it honestly: *given the same underlying
model, how much does the harness around it change the result?*

The headline, up front, and then the story of how we got there — because the
methodology turned out to be more interesting than any single number.

> **The thesis.** On correctness, frontier harnesses on a frontier model are
> indistinguishable — they all just solve the task. Where they separate is
> **efficiency**: wall-clock time (up to ~4×) and, more sharply, **tokens spent
> per solved task (up to ~8×)**. And three of four open models we tested reach
> *frontier parity* on real coding tasks — the entire 72-run open-model matrix
> cost about **$1.02**. But the most durable finding is a process one: **twice,
> a per-cell look at the raw data overturned a headline the summary numbers
> implied.** For a benchmark, that verification discipline isn't overhead. It's
> the product.

## The setup

Five harnesses — `codex`, `pi`, `opencode`, `cursor`, `devin` — each pinned to the
**same** canonical model, `gpt-5.5-medium`. Each runs headless against small,
self-contained coding tasks; a task is graded by a checker script (exit 0 = solved),
never by the harness's own claim of success. Everything is Python 3 stdlib, runs
locally, and every run is one appended JSON line so results are auditable after the
fact. No Harbor, no inspect_ai — building the measurement apparatus by hand was the
point.

## Building measurement you can trust

Before any finding, the boring infrastructure that makes a finding mean something:

- **Validated checkers.** Every task ships a `checker.sh`, a starting `workspace/`,
  and a golden `solution/`. A validator asserts the checker **fails on the
  untouched workspace** and **passes on the solution** — catching the two ways a
  grader silently lies: scoring a task solved before the agent touches it, or
  rejecting a correct answer.
- **A null control.** A built-in `null` "harness" does nothing and reports success.
  Because it never edits the workspace, every checker must fail it. A nonzero null
  score would mean a broken, too-lenient checker. It scored 0 everywhere, every
  time.
- **Wilson confidence intervals, not point estimates.** With a handful of trials,
  "2/3 = 67%" is noise. We report the Wilson 95% interval and treat two harnesses
  as different only when their intervals separate. Overlap means "not yet resolved."
- **Pilot calibration before spending.** Before a full paid matrix, a cheap pilot
  on the two fastest harnesses tells us whether a task lands in the useful 20–80%
  difficulty band. It repeatedly told us the tasks were *too easy* — which was
  itself a finding.

## The findings arc

### M3 — correctness saturates; speed separates ~4×

Three easy tasks, five harnesses, five trials. Every harness solved every task:
**15/15**, identical Wilson intervals `[0.796, 1.000]`. The only thing that
separated from the field was the null control at `[0.000, 0.204]` — confirming the
checkers discriminate. On correctness, the harnesses are a tie.

Efficiency is not. Mean wall-time per solve:

| harness | pi | cursor | devin | codex | opencode |
|---|---|---|---|---|---|
| mean s | 16.4 | 22.0 | 43.0 | 48.3 | 62.7 |

`opencode` is ~3.8× slower than `pi` on identical work. First lesson: a saturated
correctness table is not a null result — it's a **baseline**, and it points you at
the axis that actually varies.

### M3.5 — the harness tax, in tokens, spans ~8×

We wired token accounting into every adapter and asked the efficiency question in
the currency that actually costs money. Tokens per solved task:

| harness | pi | cursor | opencode | devin | codex\* |
|---|---|---|---|---|---|
| tokens/solve | 5.3k | 14.0k | 20.7k | 33.8k | 42.5k |

A **~8× spread** — wider than wall-time — and, crucially, **the token ranking is
not the time ranking.** `opencode` and `codex` finish in about the same wall-clock
(~55 s) but `codex` spends ~2× the tokens. Wall-clock and token-tax are genuinely
different efficiency axes; a harness can be quick and expensive, or slow and lean.
`pi` alone was both fast and lean. (\*`codex`'s M3.5 token figure includes cache
reads — an honest, documented caveat, not apples-to-apples.)

### M4.5 — even a 6-bug task can't separate frontier harnesses

If easy tasks saturate, make them harder. We built three genuinely harder,
partial-credit tasks — a 6-bug CI fix, a config parser with an `@include`
directive, a misleading-traceback debug — that emit a `SCORE: <0–1>` line so a
run can earn partial credit. The pilot predicted the outcome: every pilot cell
scored 1.0. It held. The four clean harnesses each scored **9/9, mean-score 1.0**.
A 6-bug fix still saturates a frontier harness. Calibrating difficulty *for the
frontier* is genuinely hard — you need longer-horizon tasks, and you risk
over-hardening into "everyone floors," which is just as uninformative. Efficiency
separated again (`opencode` ballooned to 175 s mean; `codex` heaviest at 75.8k
tokens). These saturated tasks became the **frontier baseline** for the next step.

### M4 — three of four open models reach frontier parity, for ~$1.02

The tasks that couldn't crack the frontier were the perfect probe for weaker
models. We ran four open models via first-party APIs — `glm-5.2`,
`deepseek-v4-flash`, `kimi-k2.7-code`, `glm-4.7-flash` (free) — through `pi` and
`opencode`, on the same three hard tasks. Mean score via `pi` (the clean harness):

| model | glm-5.2 | deepseek-v4-flash | kimi-k2.7-code | glm-4.7-flash |
|---|---|---|---|---|
| score (via pi) | 1.00 | 1.00 | 1.00 | 0.47 |

**Three of four match GPT-5.5-medium** on real coding tasks. Only the free, small
`glm-4.7-flash` drops below — and it does so with *genuine partial credit* (0.47),
exactly where the partial-credit design was built to bite. The whole 72-cell matrix
— 4 models × 2 harnesses × 3 tasks × 3 trials, 72 real agent runs — cost **~$1.02
total**. Frontier-comparable open-model coding for about a dollar is the efficiency
thesis in one number. And the harness signal replicated: `pi` won or tied every
model column — a **model-agnostic harness-quality** effect.

## The incidents were the methodology

Twice, the tidy summary numbers implied a story the raw cells did not support.
Both times, a per-cell look overturned it. This is the part worth stealing.

**devin, "8/9."** In M4.5, a summary said devin scored 8/9 with one "genuine
correctness miss" — a tidy anomaly worth a callout. The per-cell taxonomy said
otherwise: of nine cells, two were **900 s hangs** (the harness held its output
pipe open past task completion), one was a nonzero-exit-that-passed, and the single
"miss" was an **instant 0.99 s crash** — the same signature as an earlier adapter
regression, not a model attempt. Its token counts were internally inconsistent by
~20× (716k vs 33k on the *same* task). devin wasn't 8/9; it was *flaky*, and its
data was pulled from every ranking with an honest caveat. The anomaly scan
(positive-integer-token and adapter-error checks) is what caught it.

**opencode, the "2× harness gap."** In M4, two eye-catching gaps appeared:
`glm-4.7-flash` scored 0.47 via `pi` but 0.22 via `opencode`; `kimi` scored 1.00
via `pi` but 0.67 via `opencode`. Read naively, that's a dramatic harness×model
interaction. The failure taxonomy said: **8 of 72 cells were the same adapter bug**
— opencode's timeout handler concatenated `bytes` with `str` and crashed on every
900 s timeout. Seven of those eight were `glm-4.7-flash:opencode`; one was
`kimi:opencode`. Why so many timeouts? Because `opencode` runs these models 5–8×
slower than `pi` (789 s vs 102 s on glm-4.7-flash), so they hit the wall
constantly. The "gaps" were **infrastructure, not capability** — `kimi:pi` = 1.00
proves kimi is fully capable. We excluded the 8 exception cells from capability
scores, reported the affected combos both ways, framed the slowness as a real
*harness cost* (which it is) but not a *model* claim (which it isn't), and filed
the bug.

Neither correction came from cleverness. Both came from a rule: **classify every
cell — pass / partial / timeout / adapter-exception / zero — before you trust a
block-level average.** For a benchmark whose whole value is trustworthiness, that
per-cell discipline isn't a chore around the science. It *is* the science.

## Honest limitations

This is a shakedown, not a leaderboard. The tasks are small (n=9–15 per cell on a
single macOS host, single day) — confidence intervals are wide and some efficiency
pairs are genuinely unresolved. Correctness saturates, so it can't rank frontier
harnesses at all; the ranking claims are strictly about efficiency. Token-based
cost uses combined tokens with an assumed input/output split (a true split would
sharpen it), and one harness (`codex`) counts tokens on a different basis. `devin`
is excluded as flaky and `codex` can't run the open models (an architectural
protocol block). Every number here is reproducible from the committed datasets —
that's the point — but reproduce them before you cite them.

## What's next

- **A task factory** that reliably lands in the 20–80% band for frontier models —
  longer-horizon, partial-by-design tasks — so correctness can finally
  discriminate at the top.
- **Longitudinal drift tracking**: the same tasks over time, as models and
  harnesses ship, to catch regressions like the ones above before they reach a run.
- **Held-out tasks** to guard against the tasks leaking into training or tuning.
- **Token accounting parity** across every adapter, so the "harness tax" is
  comparable everywhere.

## Data

Every claim is backed by a committed dataset and a per-milestone write-up in
`RESULTS.md`:

- M3 — `data/m3-2026-07-02/`
- M3.5 — `data/m3.5-2026-07-02/`
- M4.5 — `data/m4.5-2026-07-03/`
- M4 — `data/m4-2026-07-03/`

Re-run any report with `python3 bench/report.py --efficiency --results-path <file>`.

The measurable takeaway: on today's tasks, the wrapper barely moves *whether* the
work gets done, but it moves *what it costs* by up to an order of magnitude — and
open models are closer to the frontier, and cheaper, than the discourse assumes.
The unmeasurable one: a benchmark is only worth its verification discipline, and
ours earned its keep by being wrong twice and catching it both times.
