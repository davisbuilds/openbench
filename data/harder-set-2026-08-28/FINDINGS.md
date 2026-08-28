# harder-set bake-off — terra vs luna on graded tasks (2026-08-28)

**Spec:** `experiments/specs/harder-set.toml` · **Host:** macbook · **Run log:** `run.log`
· **Rows:** `results.jsonl` (12 cells)

## Question

The 8-task binary core set saturates: `gpt-5.6-terra-xhigh` and `gpt-5.6-luna-max`
both went 24/24, so it can floor-check but cannot *rank* the two daily drivers.
This set is **graded** — checkers emit `SCORE: passed/total` across many edge
cases, so a plausible-but-imperfect answer lands below 1.0. Hypothesis: on graded
tasks the two arms spread out (mean score < 1.0, terra ≠ luna).

- `json-canonicalize` — spec-adherence: JSON canonical form (escaping, key order, `-0`).
- `glob-match` — algorithmic: glob semantics (segment boundaries, globstar).

Setup: native-codex, shared 5h ChatGPT quota, **serial**, 3 trials/cell.
Rank on **mean `score`**, not pass rate (a graded checker exits nonzero on any
miss, so `success` stays strictly binary while `score` carries partial credit).

## Result — hypothesis REJECTED

**12/12 cells, both arms 6/6, every single cell score 1.0.** No spread at all.

| Arm | Cells | Mean score | Mean agent_s | agent_s min/max | Theoretical cost† |
|---|---|---|---|---|---|
| gpt-5.6-terra-xhigh | 6/6 | **1.0000** | 102 | 61 / 178 | **$0.7831** |
| gpt-5.6-luna-max | 6/6 | **1.0000** | 158 | 135 / 178 | **$0.1379** |

† Price-sheet derivation from OpenRouter `/api/v1/models` list price (a **floor** —
`results.jsonl` keeps only the final saved row per cell; retried attempts uncounted,
though there were none here). `cost_usd=None` on every row by design: codex-native
arms bypass the LiteLLM proxy, so authoritative capture does not apply.

These tasks are strictly *harder to pass* than the core set — naive baselines land
0.80 (`json.dumps(ensure_ascii=True)` and `fnmatch` respectively) — but that gap is
between **naive** and **frontier**, not *within* the frontier. Both drivers clear
every edge case cleanly. **The set is nowhere near frontier difficulty.**

## The finding that DID replicate — terra's cost premium

On a fully independent task set, the core-set cost gap holds:

- **terra costs ~5.7× more than luna** ($0.78 vs $0.14) at *identical* perfect accuracy.
  (Core set: $7.48 vs $1.14, ~6.5×.)
- Counterintuitively **luna burned ~1.6× more tokens** (1.23M in / 75K out vs terra's
  749K / 37K) yet cost far less — luna's per-token list price is ~10× cheaper, which
  swamps the volume difference.
- **terra is faster** (102s vs 158s mean); the xhigh reasoning effort buys latency at
  a steep price multiple.

**Daily-driver takeaway:** on any task either can solve, **luna is the correct
default** — same answer, ~1/6th the cost, modest latency cost. terra's premium only
earns out on tasks luna actually *fails* — and no task built so far separates them.

## What this tells the next tier

A discriminating task must make an imperfect frontier answer the *expected* outcome,
not merely make a naive baseline fail. See `docs/project/BACKLOG.md` →
"Frontier-difficulty task tier" for the design criteria this run motivated.
