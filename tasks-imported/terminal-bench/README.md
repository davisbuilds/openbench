# Terminal-Bench imported tier (frontier-hard)

Five frontier-hard tasks imported from **Terminal-Bench**
(https://github.com/laude-institute/terminal-bench), a benchmark of hard,
self-contained terminal tasks hand-crafted by academic and industry researchers.

- **Upstream commit**: `1a6ffa9674b571da0ed040c470cb40c4d85f9b9b`
- **License**: Apache-2.0 (upstream `LICENSE`). Each task directory carries a
  `PROVENANCE.md` recording the upstream task name, commit, license, and the
  exact conversion changes. Upstream "canary" strings are intentionally omitted
  from the imported files.

## Scored separately

This tier lives under `tasks-imported/` and is **scored separately from the core
`tasks/` tier** — `validate_tasks.py` and the benchmark runner treat each
`tasks-imported/<collection>` as its own maintainer-curated collection and never
blend it into core results. Treat these five as a distinct, harder difficulty
band; do not average their scores with core or with the exercism tier.

## How to run

These tasks are selected and adapted for OpenBench's **docker lane**. The agent
runs in the `openbench-harness:latest` image (python3.11 stdlib + node + bash;
**no pip, no compilers, no GPU, no network**), so run them with `--exec docker`:

```
python bench/run.py --task terminal-bench/<name> --harness <h> --exec docker \
    --tasks-dir tasks-imported
```

The checkers are pure-`python3` stdlib + bash and also run under the host-side
`validate_tasks.py`. No task needs the network at check time.

## Tasks

| Task | Upstream difficulty | What the agent must do |
|------|--------------------|------------------------|
| `feal-differential-cryptanalysis` | hard | Implement a differential chosen-plaintext attack (`attack.py`) that recovers `key[5]` of a FEAL-like cipher. |
| `llm-inference-batching-scheduler` | hard | Produce shape-aware batching plans that cover all requests and beat tight cost / pad-ratio / p95-latency / sequential-timecost thresholds with ≤8 unique tensor shapes. |
| `schemelike-metacircular-eval` | medium | Write a metacircular evaluator `eval.scm` that interprets a scheme-like language — and can interpret itself. |
| `cancel-async-tasks` (**DROPPED 2026-07-11**) | hard | Implement `run_tasks(tasks, max_concurrent)` in `run.py` with correct asyncio concurrency limiting and cleanup that still runs when a `SIGINT` cancels the run. Dropped: the checker is load-sensitive — correct solutions scored FAIL under host load and PASS idle on three separate occasions, surviving a readiness-based rewrite. See `cancel-async-tasks/DROPPED.md`. |
| `count-call-stack` | easy | Parse a 4 MB profiler-stack log and emit the top-10 call sites in an exact text format. (A deterministic, low-variance anchor.) |

## Selection rationale & difficulty evidence

The dominant constraint is the fixed minimal image: every task must be solvable
and checkable with **python3 stdlib + bash only**, deterministic, and offline.
That eliminates the large majority of Terminal-Bench's 241 tasks (which each ship
their own heavy per-task Dockerfile — torch, R, qemu, compilers, biopython,
rdflib, cvxpy, primer3, ...). The five here are the algorithmically hard,
dependency-light tail. Difficulty evidence: TB's per-task `difficulty` field
(four of the five are `hard`/`medium`; only `count-call-stack` is `easy`) plus
the intrinsically hard task categories (cryptanalysis, self-hosting interpreter,
threshold-constrained optimization). Full selection notes and the list of
rejected candidates are in `.proofs/worker-tb/selection_notes.md`.

## Conversion modifications (common to all)

- `instruction.md` is the upstream instruction prose, adapted so paths are
  relative to the working directory instead of `/app/...`.
- Checkers are **pure-stdlib re-implementations** of each task's upstream pytest
  `tests/test_outputs.py` (upstream runs pytest under `uv`; the minimal image has
  neither). Semantics are preserved; see each `PROVENANCE.md` for specifics.
- `solution/` holds a known-good solved workspace: for four tasks the reference
  is extracted verbatim from the upstream `solution.sh`; for
  `llm-inference-batching-scheduler` the reference optimizer was run to generate
  the golden plan files, and for `count-call-stack` the golden is the upstream
  reference output.
- **`feal-differential-cryptanalysis` changes the verification basis**: upstream
  checks the attack against a C build of FEAL (`setup.py build_ext`); with no
  compiler in the image, the checker verifies against the equivalent reference
  **pure-Python** cipher. Because the attack is randomized it is run on
  5 independent random keys (reference solution: 20/20 in testing).
- **Checker-owned oracles**: wherever grading depends on a reference artifact
  (feal's cipher, schemelike's interpreter and test programs, the batching
  scheduler's cost model and input hashes, count-call-stack's golden output),
  the checker loads its own copy from `checker_data/` in the read-only task dir
  — never the agent-editable workspace copy — so doctoring workspace files
  cannot influence the score. Residuals are documented per-task in
  `PROVENANCE.md`.

## Caveats

- `schemelike-metacircular-eval`: the checker runs ~63 scheme programs through
  both `interp.py` and the agent's `eval.scm` (with self-hosting for a few).
  Isolated docker checker time was **~40 s** on the dev machine — comfortably
  under the 120 s default `--checker-timeout`, but the slowest checker in this
  tier; a heavily loaded machine may want a higher checker timeout. Also, upstream
  keeps a held-out `shadow_test/` set; here it is included in the workspace, which
  slightly lowers difficulty vs. upstream.
- `feal-differential-cryptanalysis`: the checker is inherently randomized (fresh
  random keys each run). It is robust (20/20 in testing, 5/5 required per run) but
  is not bit-for-bit deterministic like the others.
- `count-call-stack` is `easy`; it is included as a deterministic, low-variance
  anchor rather than as a frontier-hard task.
