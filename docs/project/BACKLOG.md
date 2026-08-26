# Backlog

Living list of **future** design gaps, tech debt, and better ways to do a thing
noticed during normal execution on this fork. Fix simple, quick, or blocking
issues inline; capture only durable follow-ups worth revisiting cold. Not a
commitment for the active task. Add an item only when it cannot be fixed inline
and represents recurring friction, meaningful risk or cost, an unresolved
decision, or a concrete trigger.

This is a fork of `minghinmatthewlam/openbench` under `~/Dev/_forks/`. Capability
that belongs upstream is tracked as an upstream issue/PR (linked below);
fork-local convenience stays here.

Convention (matches the portfolio pattern): each item has **What** (the
friction), **Why or evidence**, and optionally **Next** (the smallest action
that makes it actionable) or **Revisit when** (an intentional gate). Cite a
number or label a claim *hypothesis, unmeasured* — entries get read back later
as fact. When an item ships it **leaves this doc** (record it in a roadmap /
the PR, not as a "resolved" note here).

---

## Open

### Runner throughput

#### Optional parallel execution for the matrix / legacy runners
- **What**: both runners are strictly serial — `matrix_queue.run_matrix` pops one
  cell at a time (`pending.pop(0)`) and blocks on a synchronous `run_runner`
  (`proc.wait`); the legacy per-cell runner is serial too.
- **Why it matters** (*measured 2026-08-26*): a 3-model × 19-task × 3-trial
  matrix (171 cells) runs fully serialized even though arms are independent — the
  wall-clock is ~3× what concurrent arms would cost. This is the single biggest
  throughput lever.
- **Why it is not a quick fix**: parallelism must not perturb the per-cell
  timing/token measurements that are openbench's whole point, needs results-JSONL
  write-safety, and needs per-provider concurrency caps (uncapped fan-out trades
  serialization for self-inflicted 429s). Prototype behind a **default-off**
  `--workers` flag before committing to it.
- **In flight** (branch `feat/matrix-parallel-arms`): arm-level `--workers`
  landed default-off. The per-cell decision was extracted to a pure core
  (`decide_cell_outcome`), the loop rewired through it, and a per-arm drain runs
  arms concurrently behind one lock (shared results + queue-state files only);
  each cell writes an arm-private part file merged as a whole-line append, so
  concurrent children can't interleave. run_matrix got its first end-to-end net
  (fake-runner integration tests: serial/parallel coverage, real overlap,
  no-torn-lines). **Still owed before trusting it for a scored run**: the
  empirical timing-contamination gate -- one arm solo vs the same arm under
  concurrency, wall-time delta must stay within noise, else parallel runs
  corrupt the latency numbers the benchmark exists to produce. Until that gate
  passes, use `--workers` only for throughput on distinct-provider arms, not for
  latency-sensitive comparisons.
- **Timing gate result** (*measured 2026-08-26*, `experiments/analyze_gate.py`,
  glm-5.3-flash solo vs under 3-way concurrency): **CONDITIONAL PASS**.
  Local/CPU contention is negligible -- `t_env_setup_s` and `t_checker_s` (the
  local-CPU parts) are unchanged. BUT on the short, turn-heavy `make-it-run`
  task, ALL THREE distinct-provider arms inflated `t_agent_s` together (glm
  27s->57-147s, deepseek->62-103s, minimax->62-107s). Different providers moving
  in lockstep isolates the cause to the **single shared LiteLLM bridge**
  (127.0.0.1:4141, one process): it serializes concurrent agent round-trips, so
  turn-heavy tasks pay a latency penalty. Longer few-turn tasks stayed within
  noise (-26%..+18%). **Verdict: `--workers` is safe for THROUGHPUT (coverage
  was identical, every cell solved) but NOT for scored LATENCY comparisons until
  the bridge is per-arm isolated** (separate port per concurrent arm, or a
  bridge with real worker concurrency). Directly implicates the "Bridge
  lifecycle" item below.
- **Next**: tracked upstream as
  [minghinmatthewlam/openbench#45](https://github.com/minghinmatthewlam/openbench/issues/45).
  For latency-safe parallelism, pair `--workers` with per-arm bridge isolation
  first. Hold the upstream PR until then and until the maintainer signals
  interest.

### Extensibility

#### Externalize the adapter `OPEN_MODELS` registry to config — *codex done, others pending*
- **What**: adding an open / BYO model meant editing the adapter's `OPEN_MODELS`
  dict in code plus a matching `bridge/config.yaml` route.
- **Why it matters** (*measured 2026-08-26*): wiring the OpenRouter bake-off
  meant editing `codex.py` **6×** in one session. Per-model code churn is also a
  merge-conflict surface against upstream on a fork.
- **Done locally**: `obench/open_models_config.py` loads operator routes from
  `$OPENBENCH_OPEN_MODELS` (else `~/.openbench/open_models.toml`) and merges them
  over an adapter's built-ins; wired into the **codex** adapter (commit
  `7fbc975`, on `main`).
- **Next (NOT a drop-in)**: the remaining adapters carry *heterogeneous* entry
  schemas — `pi` adds `context_window`/`thinking`/`compat`/`thinkingLevelMap`,
  `opencode` uses `variant` not `effort`, `claude` has no `provider`, `grokbuild`
  adds `base_url_env`/`proxy_route`/`subscription_bridge`. The current loader
  validates against codex's `_REQUIRED_KEYS` and normalizes to codex's shape, so
  it would reject their entries and strip their fields. Generalizing needs (a) a
  schema-agnostic loader (pass keys through; optional per-adapter
  `required_keys`), and (b) per-adapter config namespacing (`[codex.models.X]`
  vs `[pi.models.X]`) since one flat `[models.X]` table can't satisfy two
  schemas. Low priority until we actually run those harnesses (today only codex).
  Upstream contribution tracked as
  [minghinmatthewlam/openbench#46](https://github.com/minghinmatthewlam/openbench/issues/46)
  — hold the PR until the maintainer signals interest (issue-first, per plan).

### Operator ergonomics

#### Bridge lifecycle is manual and foreground
- **What**: the open-model LiteLLM bridge must be started by hand before any
  open-model run; the runner only TCP-probes it and returns SETUP-NEEDED if down.
- **Why it matters** (*observed 2026-08-26*): restarted the bridge ~5× in one
  session (each new model route needs a reload). Friction, and an easy way to run
  a whole matrix against a stale config.
- **Why it may stay local**: the foreground/human-managed design is intentional —
  the bridge injects real provider keys upstream and has no ingress auth (see the
  security note in `openmodel_bridge.sh`). A supervised-bridge option would have
  to preserve that boundary.
- **Next**: fork-local `bridge up`/`down` helper with a config-hash check + health
  wait, if the friction recurs. Keep local unless it generalizes cleanly.

#### Per-arm bridge isolation — the latency-safe unlock for `--workers`
- **What**: one shared LiteLLM bridge (127.0.0.1:4141) serves every open-model
  arm. Fine serially; under `--workers > 1` it becomes the parallelism
  bottleneck.
- **Why it matters** (*measured 2026-08-26, timing gate*): with 3 arms on
  distinct providers running concurrently, all three inflated `t_agent_s`
  together on the turn-heavy `make-it-run` task (glm 27s→57-147s, and likewise
  for minimax/deepseek) — different providers moving in lockstep isolates the
  cause to the shared bridge serializing round-trips. This is exactly why the
  gate rated `--workers` throughput-safe but *not* latency-safe.
- **Next**: give each concurrent arm its own bridge instance (a port per arm,
  templated config) OR run the bridge with real worker concurrency, then re-run
  the timing gate. Until then `--workers` stays throughput-only (a runtime
  warning now says so). Unblocks latency-safe parallelism for
  [#45](https://github.com/minghinmatthewlam/openbench/issues/45).

### Security posture

#### Local-mode checker runs unsandboxed on the host — task-trust boundary
- **What**: in `exec_mode = "local"`, the agent runs under codex's
  `workspace-write` Seatbelt sandbox (writes confined to the workdir + temp, no
  shell network), but `run_checker` (`obench/run.py:1265`) executes the task's
  `checker.sh` as a plain host `subprocess` — full host env, full host
  privileges, **no sandbox**. A second, quieter channel: the agent's sandbox
  blocks outside *writes* and shell *network*, but allows broad *reads*, and
  anything read enters the model context and is sent to the provider — so a
  prompt-injected task could read a host secret and exfiltrate it via the model
  API even though the shell cannot `curl` it out.
- **Why it matters** (*reviewed 2026-08-26*): local-mode safety therefore rests
  entirely on the task (both `instruction.md` and `checker.sh`) being trusted.
  The vendored core + exercism sets were read and verified clean (checkers call
  only `python3`; prompts are benign) — but any imported/community task set is
  arbitrary host-privileged code at check time.
- **Next**: (a) document that untrusted task sets MUST run under `--exec docker`
  (checker runs in the disposable container, agent reads can't see the host
  home); (b) consider a preflight that warns when running non-vendored tasks in
  local mode; (c) optionally wrap the local checker in the same Seatbelt profile
  as the agent. Decision/risk item — no code change until we actually import an
  untrusted set.

---

## Tracked elsewhere (in flight — will leave this doc on merge)

- **Classifier: measured no-work incomplete runs → `infra`, not `wrong_answer`** —
  open PR [#43](https://github.com/minghinmatthewlam/openbench/pull/43).
- **Matrix runner: per-group `timeout` override + throttle-timeout retry cap** —
  on branch `feat/matrix-runner-improvements` (bundles the already-committed
  local-group preflight crash fix + `allow_version_drift` waiver). Both born from
  measured pain: a single throttled `webcore` cell burned ~947 requests / ~9h
  re-running full 30-min timeouts because a throttle-dominated timeout is
  classified `rate_limited` and retried with no cumulative wall-time cap.
