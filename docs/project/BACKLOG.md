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
- **Next**: tracked upstream as
  [minghinmatthewlam/openbench#45](https://github.com/minghinmatthewlam/openbench/issues/45).
  Prototype locally behind a flag if the direction is welcomed.

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
