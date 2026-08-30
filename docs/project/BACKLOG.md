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

### Benchmark discrimination

#### Frontier-difficulty task tier — the graded set does not rank terra vs luna
- **What**: build a task tier whose *expected frontier score is < 1.0*, so the
  two daily drivers (terra-xhigh, luna-max) can be ranked on capability, not just
  cost.
- **Why it matters** (*measured 2026-08-28*, `data/harder-set-2026-08-28/`): the
  binary core set saturates (both 24/24) and the first graded set
  (`json-canonicalize`, `glob-match`) *also* saturated — both arms 12/12 at mean
  score **1.0**, zero spread. Naive baselines land 0.80 on those tasks, so the set
  is harder to *pass* than the core, but the gap is naive→frontier, not
  within-frontier.
- **Root cause of saturation** (the design lesson): both tasks were (a) fully
  specified, (b) small/self-contained single-file pure logic, (c) *textbook
  algorithms in the training distribution* (JSON canonicalization, glob), and (d)
  their "hard" edge cases were exactly the ones a well-read model already
  anticipates. Per-case hit rate for a frontier model was ~99.9%, so mean score
  pinned at 1.0. **Making a naive baseline fail is not the same as making a
  frontier answer imperfect.**
- **Design principle**: partial-credit needs *per-case difficulty × case count*.
  Aim for a ~85–95% per-case frontier hit rate over 30+ *independent* cases, so
  the mean lands ~0.9 with enough variance to separate arms. Reach that by leaving
  the memorized-algorithm distribution: invented/underspecified-but-objective
  DSLs, clause *interactions* (not individually memorable edge cases), or
  long-horizon tasks graded by many regression buckets where errors compound.
- **Candidate axes** (objective graders only — avoid taste-test scoring):
  (A) *scale-of-requirements* — dense spec, many interacting clauses, score =
  fraction satisfied (e.g. cron-eval with `L`/`W`/`#`/`?`, semver-range resolver,
  RFC-3986 URI normalizer); (B/E) *long-horizon build/debug* in a realistic
  codebase, graded by test buckets — where terra's expensive extra reasoning is
  most likely to earn out; (F) *quality gradient* — correct AND under a
  perf/size/token budget.
- **Open meta-question**: is the goal (i) capability *separation* (find ANY task
  where terra beats luna — if none exists across a genuinely hard tier, "luna
  dominates on cost" is itself the decision-relevant finding) or (ii) absolute
  *difficulty calibration* (both land < 1.0, useful as a standing eval to track
  future models even if today's two arms tie)? Pick before building the tier.
- **Next**: build ONE axis-A probe + ONE axis-B/E probe, run them before
  investing in a full tier. If even axis-A can't drop either arm below 1.0, the
  discrimination problem is genuinely hard → pivot to long-horizon or accept
  capability-parity.

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

#### No validation that `bridge/config.yaml` and adapter `OPEN_MODELS` agree
- **What**: routing a model requires **two** places to agree — the adapter's
  `OPEN_MODELS` entry (provider/`env_key`/`model_id`, which drives the key
  preflight and bridge `model_id`) and the bridge `config.yaml` `model_name` →
  upstream slug. Nothing checks they match, and the adapter's built-in silently
  wins over `config.yaml`.
- **Why it matters** (*hit 2026-08-29, adding kimi-k3 + nemotron-3-ultra*):
  `config.yaml` was given a `kimi-k3` → `openrouter/moonshotai/kimi-k3` route, but
  the codex adapter *already ships* a built-in `kimi-k3` → **direct Moonshot**
  (`MOONSHOT_API_KEY`) entry (`codex.py:172`). The config looked correctly wired,
  yet every cell failed `SETUP-NEEDED: export MOONSHOT_API_KEY` — the built-in
  shadowed it. Fix was a `~/.openbench/open_models.toml` override (config wins on
  collision). This is exactly the cross-site-consistency defect class the
  `am-consistency` task tests: change one place, miss the parallel place that must
  agree. A new model added only to `config.yaml` is a latent trap.
- **Next**: a `bridge` (or `doctor`) preflight that cross-checks — every
  `config.yaml` `model_name` should have a matching adapter/override entry whose
  `model_id` equals it and whose provider/`env_key` are consistent with the
  route; warn on any `model_name` present in one surface but routed differently in
  the other. Cheap to write, kills a whole confusing failure mode.

### Operator ergonomics

#### Config/setup errors consume the full retry budget instead of failing fast
- **What**: a deterministic config error — `SETUP-NEEDED: export <KEY>` (missing
  key) or `unsupported-model` (name not in `OPEN_MODELS`) — is retried like a
  transient failure and burns the arm's whole `infra` budget before pausing.
- **Why it matters** (*observed 2026-08-29*): the mis-routed kimi-k3 and
  not-yet-registered nemotron arms each retried **3×** and paused the arm, when
  the very first attempt already carried a verdict that no retry could change.
  Wasted wall time and noisy `exhausted`/`PAUSED` state for a one-line fix.
- **Next**: classify `SETUP-NEEDED` / `unsupported-model` as **non-retryable
  config errors** (0 retries, fail fast), and — better — surface them in a
  *planning-time* preflight (validate every arm's key + registry membership
  before the queue runs) so the run never starts a doomed cell. `ArmState`
  already has a `config_error` field to hang this on.

#### Queue-state ledger overrides `results.jsonl` for exhaustion — desync on prune
- **What**: `results.jsonl` is documented as the resume source of truth, but
  `queue-state.json` separately persists `exhausted_cells` + `retry_counts` and is
  restored verbatim at launch (`matrix_queue.py:1008-1014`). Removing a cell's
  rows from `results.jsonl` to force a re-run does **not** clear its ledger
  exhaustion, so the runner keeps skipping it.
- **Why it matters** (*hit 2026-08-29*): after fixing kimi-k3's routing and
  pruning its 9 failed rows from `results.jsonl`, relaunch still reported it
  `EXHAUSTED` and never re-ran it — the ledger remembered. The only reset was
  deleting the whole `queue-state.json` (blunt: also drops unrelated arms' state).
  This contradicts the "results.jsonl is authoritative" contract.
- **Next**: either (a) an `obench matrix --requeue <arm|cell>` that clears a
  cell's exhaustion + retry counts, or (b) reconcile the ledger against
  `results.jsonl` on load — if a cell's rows are gone, drop it from
  `exhausted_cells`/`retry_counts` so results-file edits actually take effect.

#### Bridge lifecycle is manual and foreground — *shipped (fork-local) 2026-08-27*
- **What**: the open-model LiteLLM bridge must be started by hand before any
  open-model run; the runner only TCP-probes it and returns SETUP-NEEDED if down.
- **Why it mattered** (*observed 2026-08-26*): restarted the bridge ~5× in one
  session (each new model route needs a reload). Friction, and an easy way to run
  a whole matrix against a stale config.
- **Shipped**: `obench bridge up|down|status` (`obench/bridge_cli.py`, 37 tests)
  wraps `openmodel_bridge.sh` with a sha256 config-hash staleness check + health
  wait. `up` is a no-op when a healthy bridge's hash matches; warns + refuses on
  a stale hash; `status` reports in-sync / STALE / untracked. The foreground/
  human-managed key boundary is preserved (it wraps the existing script, adds no
  ingress). Stays fork-local unless it generalizes cleanly.
- **Not yet validated**: launching a real LiteLLM was not exercised in tests (no
  litellm venv in CI); `down`/`status` verified against no running bridge. One
  live `up` against the real bridge would close this.

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

#### Cost telemetry — *implemented 2026-08-27, pending live validation*
- **What**: rows record a full vendor token split (`token_basis: vendor_split`).
  `experiments/analyze_cost.py` derives theoretical cost post-hoc = tokens × a
  per-token price sheet pulled from OpenRouter `/api/v1/models` (it lists both our
  OpenRouter arms *and* the `openai/gpt-5.6-terra|luna` codex base models, so even
  subscription-run codex arms get a real API-list-price figure).
- **Shipped (authoritative capture)**: rows now carry `cost_usd`/`cost_source`.
  `proxy.extract_cost()` reads OpenRouter's own `usage.cost` (bridge config sends
  `extra_body.usage.include` on the six openrouter/ entries) and falls back to
  LiteLLM's `x-litellm-response-cost` header. `analyze_cost.py` prefers a row's
  captured cost over the price-sheet estimate (`effective_row_cost`). Codex-native
  arms bypass the proxy → `cost_usd=None` (price-sheet only, by design).
- **Why post-hoc derivation is still a floor** (*observed 2026-08-27*):
  `results.jsonl` keeps only the final saved row per cell, so throttled/retried
  attempts are uncounted — known-priced OpenRouter floor ~$0.45 vs ~$1.62 on the
  dashboard. Authoritative capture fixes attribution going forward but does not
  retro-count past discarded attempts.
- **Live validation ran — and capture is NOT landing** (*observed 2026-08-29,
  `results/am-consistency/`*): the `am-consistency` Pareto run exercised four paid
  OpenRouter arms through the bridge (minimax-m3, glm-5.3-flash,
  deepseek-v4-flash-0731, nemotron-3-ultra) and **every row came back
  `cost_usd=None` / `cost_source=None`** — neither OpenRouter's `usage.cost` nor
  the `x-litellm-response-cost` header reached the row. Codex-native arms are
  `None` as designed; the open arms should NOT be. This breaks the whole "×cost"
  axis of the Pareto study (we fall back to price-sheet derivation only). So the
  authoritative-capture piece is **not** upstream-worthy yet — it regressed or was
  never exercised end-to-end on this path.
- **Suspects to check** (unverified): (a) codex's open-model requests take the
  adapter's bridge path but bypass the *counting proxy* (`OPENBENCH_PROXY` unset
  in this run), and `extract_cost()` may live only on the proxy path, not the
  bare-bridge path; (b) LiteLLM's Responses→Chat bridge may strip `usage.cost`
  out of the projected response; (c) the header may not survive the bridge hop.
  First triage: capture one raw upstream response body on the bridge and grep for
  `cost`. **Still needs**: a codex-native run confirming `cost_usd` is `None` by
  design (that half is presumably fine).

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
