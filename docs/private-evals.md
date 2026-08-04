# Private Harbor-native evaluations

New OpenBench coding tasks are Harbor-native. A private repository commits
human-authored benchmark intent and local Harbor task directories under
`.openbench/`; Harbor will remain responsible for execution and task locking.
Legacy OpenBench checker tasks are migration compatibility, not the authoring
default.

## Initialize

OpenBench itself requires Python 3.11+ and has no runtime Python dependencies.
From the private repository root:

```bash
obench init
```

The command is offline and idempotent. It does not contact Harbor, Docker, a
model provider, or an authentication service. Existing files are never
overwritten.

| Path | Commit? | Purpose |
|---|---:|---|
| `.openbench/openbench.toml` | yes | Project paths and the default suite |
| `.openbench/suites/default.toml` | yes | Human-authored benchmark intent |
| `.openbench/tasks/example-greeting/` | yes | Small Harbor schema 1.4 example |
| `.openbench/profiles/local-codex.toml` | yes | Credential-free profile scaffold |
| `.openbench/jobs/` | no | Local Harbor job state |
| `.openbench/results/` | no | Local normalized results |
| `.openbench/trajectories/` | no | Private ATIF trajectories |

Parse the generated suite without running anything:

```bash
python3 -c 'from obench.suite import load_suite; load_suite(".openbench/suites/default.toml")'
```

Suite execution is intentionally not wired to `obench run` in this change.

## Author local tasks

Each child of `.openbench/tasks/` is a normal Harbor task directory:

```text
my-task/
  instruction.md
  task.toml
  environment/
    Dockerfile
    app/
  tests/
    test.sh
  solution/
    solve.sh
```

Use Harbor task schema 1.4. Keep the starting environment unsolved, put the
deterministic oracle in `solution/solve.sh`, and make the verifier write
`reward.json` or `reward.txt`. The generated greeting task is deliberately
small enough to inspect end to end.

Private and OpenBench-owned tasks stay as local Harbor directories. They do not
need to be repackaged as an external dataset. Paths in `suite.toml` are relative
to the project root and may not escape it or traverse symlinks.

## Reference external task sets

Do not vendor external task collections into the private task tree. Add an
immutable Harbor package or dataset reference:

```toml
[[task_sets]]
id = "external"
kind = "harbor"
name = "org/package"
ref = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
git_commit = "0123456789abcdef0123456789abcdef01234567"
subdir = "tasks/coding"
```

`ref` must be a SHA-256 digest, exact 40/64-hex commit, or exact semantic
version. Floating labels such as `latest`, `main`, and version ranges fail
validation. Optional source provenance uses an exact `git_commit`; `subdir`
requires that commit and must be a safe relative path.

See [Harbor suites](harbor-suites.md) for the complete v1 contract.

## Credentials and local evidence

Do not put credential files, tokens, auth paths, job directories, result paths,
or trajectory paths in `suite.toml`. The closed suite schema rejects those
keys. Profile scaffolds are committed intent only; credentials remain
host-managed at execution time.

`.openbench/.gitignore` excludes jobs, results, and trajectories because these
can contain private source, prompts, tool output, and provider evidence.

## Legacy migration

The old `instruction.md + workspace/ + checker.sh` format remains available
only for existing task migration:

```bash
obench init --legacy-task old-task --from path/to/fixture
# deprecated spelling, same explicit legacy behavior:
obench init --task old-task --from path/to/fixture

obench validate --tasks-dir .openbench/legacy-tasks
```

Legacy scaffolds land under `.openbench/legacy-tasks/`; they are never inserted
into the Harbor-native default suite. Migrate by creating a Harbor 1.4 task
under `.openbench/tasks/`, moving the starting files into `environment/app/`,
mapping `checker.sh` to `tests/test.sh`, and replacing the golden overlay with
`solution/solve.sh`.
