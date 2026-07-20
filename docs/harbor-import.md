# Harbor import bridge

Import [Harbor](https://www.harborframework.com/)-format tasks into OpenBench
task directories so you can run them under OpenBench harness adapters, metering,
and reporting.

```bash
obench import harbor --from /path/to/harbor-task --out ./tasks-imported --collection tb2
obench import harbor --from '/data/harbor-datasets/tb2/*' --out ./tasks-imported --collection tb2
```

This is the reverse of [`docs/harbor-export.md`](harbor-export.md)
(`obench export harbor`). Use **export** to run OpenBench suites on Harbor cloud
sandboxes; use **import** to pull Harbor / Terminal-Bench-style tasks into the
OpenBench comparison layer.

The importer never runs Docker and never grants network access to imported
scripts (`solve.sh` materialization uses `env -i` when attempted).

## What gets imported

For each Harbor task directory the importer writes:

```
<out>/[<collection>/]<name>/
  instruction.md              # copied (unchanged)
  workspace/                  # staged from environment/ Dockerfile COPY/ADD
  checker.sh                  # wraps Harbor tests/test.sh → exit / SCORE:
  checker_data/harbor-tests/  # Harbor tests/ (verifier-owned, not workspace)
  solution/                   # when materializable (see below)
  PROVENANCE.md               # source, schema, license reminder, attention
  REQUIREMENTS.md             # when DOCKER-REQUIRED / base image guidance
  harbor-import.json          # machine-readable import marker
```

## What imports cleanly vs needs attention

| Situation | Result |
|---|---|
| Dockerfile is `FROM` + `WORKDIR` + local `COPY`/`ADD` only | Clean workspace staging; local `--exec local` often works |
| Dockerfile has `RUN` installs, multi-stage, remote `ADD`, `CMD`/`ENTRYPOINT` | Still imported, marked **DOCKER-REQUIRED**; prefer `--exec docker` with a matching image (`REQUIREMENTS.md`) |
| Harbor ships non-`.sh` files under `solution/` | Copied into OpenBench `solution/` (covers OpenBench→Harbor→OpenBench round-trips) |
| Only `solution/solve.sh`, deterministic local writer | Run under `env -i` (no network) to materialize `solution/` |
| `solve.sh` needs container/network/package installs | Skip `solution/`; **needs-manual-solution**; polarity validation skipped |
| `instruction.md` mentions `/logs/verifier` or `reward.txt`/`reward.json` | **Import refused** for that task (grading internals must not leak to agents) |

After import, polarity validation runs automatically on tasks that have
`solution/`. The CLI prints a summary table:

`imported OK` / `DOCKER-REQUIRED` / `solution-materialized` / `needs-manual-attention`

Exit code is nonzero if **zero** tasks imported cleanly (non-docker-required +
solution materialized + polarity pass).

## Reward mapping

Harbor verifiers write `/logs/verifier/reward.txt` or `reward.json`. The
generated OpenBench `checker.sh`:

1. Copies `checker_data/harbor-tests/` to a temp dir
2. Rewrites absolute `/logs/verifier` and `/tests` (and common `/app` workdir)
   paths for local execution
3. Sets `VERIFIER_LOGS_DIR` and runs Harbor `test.sh` with cwd = workspace
4. Reads the reward with the **same field preference as the exporter**:
   - prefer `reward.json` over `reward.txt`
   - JSON object keys: `reward`, then `score`, then `accuracy`, else first numeric
5. Maps to OpenBench:

| Harbor reward | OpenBench checker |
|---|---|
| `>= 1.0` | exit 0 (fully solved) |
| `0 < reward < 1` | print `SCORE: <reward>`, exit 1 |
| `<= 0` or missing | exit 1 |

## License responsibility

**You must verify the upstream dataset / task license before publishing or
redistributing imported tasks.** The importer records a reminder in each
`PROVENANCE.md` but does not inspect or grant rights. Harbor is Apache-2.0;
Terminal-Bench and Harbor registry datasets carry their own terms.

## Combined story with export

```bash
# OpenBench → Harbor (cloud sandboxes)
obench export harbor --task make-it-run --out ./harbor-out
harbor run -p ./harbor-out/make-it-run -a oracle

# Harbor → OpenBench (harness comparison / metering / report)
obench import harbor --from ./harbor-out/make-it-run --out ./tasks-imported --collection from-harbor
obench validate --tasks-dir ./tasks-imported/from-harbor
obench run --tasks-dir ./tasks-imported --task from-harbor/make-it-run --harness pi
obench report
```

Round-trip (export then import) is covered by the unit suite: a core task
exported to Harbor and imported back must preserve polarity (untouched fails,
solution overlay passes with the same scores).

## Real-world findings (Terminal-Bench 2.0)

Exercised against tasks from
[laude-institute/terminal-bench-2](https://github.com/laude-institute/terminal-bench-2)
(Harbor layout). Patterns the importer now handles:

| Upstream pattern | Example task | Importer behavior |
|---|---|---|
| `FROM` + `WORKDIR` only (empty workspace) | `cancel-async-tasks` | Not DOCKER-REQUIRED; empty `workspace/` is OK |
| `COPY asset /app` + solve writes `/app/...` | `code-from-image` | Stages asset; rewrites `/app` in `solve.sh` for local materialize |
| Relative `COPY setup.sh ./` then later `WORKDIR` change | `fix-git` | Resolves COPY against WORKDIR **at that line** |
| `COPY` outside agent workdir (`/etc/nginx/...`) | `git-multibranch` | DOCKER-REQUIRED; best-effort basename staging |
| Build-time `RUN` (compile, curl fonts, `rm` sources) | `chess-best-move`, `extract-elf` | DOCKER-REQUIRED; staged COPY sources only (not RUN artifacts) |
| Multi-line `RUN apt-get install -y \` … | `git-multibranch` | Joins `\` continuations; package hints stop at `&&` |

**Still unsupported / residual risk (no Docker run on import):**

- Harbor `tests/test.sh` that `apt-get` / `curl | sh` / `uvx` at check time need network or a matching image; static import succeeds but local polarity may fail offline.
- `solution/solve.sh` that installs packages (`apt`, `pip`) is skipped (needs-manual-solution).
- Build products created only by Dockerfile `RUN` (compiled binaries, generated images) are not materialized into `workspace/`.
- JSON-form `COPY`, `COPY --from=`, remote `ADD`, multi-stage images remain DOCKER-REQUIRED without full staging.

## Format sources (Jul 2026)

- Harbor task layout / rewards:
  https://www.harborframework.com/docs/tasks
- Terminal-Bench 2.0 tasks (Harbor format):
  https://github.com/laude-institute/terminal-bench-2
- OpenBench exporter (inverse contract): [`docs/harbor-export.md`](harbor-export.md)
