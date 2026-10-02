# Frozen repair context and checkouts

Repair suites can supply a reviewed snapshot of development guidance independently
of the task and runtime image. This is supported by the isolated Codex lane.
Native model-specific base instructions remain intact. The bundle supplies global
instructions, a selected skill catalog and its supporting resources; project
instructions come from the historical checkout.

## Prepare and freeze context

Prepare an explicit directory with this layout:

```text
capture/
  codex/AGENTS.md
  codex/skills/<name>/SKILL.md
  codex/skills/<name>/references/...
  codex/skills/<name>/scripts/...
  resources/...
```

Compose the intended global/workspace guidance into `codex/AGENTS.md`. Keep
standing preferences resident, with skills discovered on demand. Copy the full
supporting tree of each selected skill. Review references: include applicable
guides, map their paths to container destinations, and explicitly identify
unavailable host services or excluded resources. Record original and adapted
hashes in private provenance. Do not copy an entire harness home.

The operator must review content for credentials, private context unrelated to
the task, answer-bearing notes and historical solutions. The archive validator
checks structure and integrity; it cannot decide whether prose leaks an answer.
Global guidance should state the container environment and explicitly ask for
relevant verification. Do not replace the native model prompt to achieve this.

```bash
python -m obench.frozen_context context results/capture \
  --output results/context-v1.tar
python -m obench.frozen_context verify results/context-v1.tar \
  --kind context --sha256 <returned-sha256>
```

Outputs are exclusively created, deterministic uncompressed tar archives with
per-file hashes, lengths and normalized executable modes. Loading requires the
whole-archive SHA256. Bounds are 64 MiB archive, 32 MiB payload, 8 MiB per file
and 8,192 files. Links, special files, traversal, duplicate entries, path
collisions and supplied `.git` history are rejected. Context excludes harness
configuration, authentication and session directories. Use physical paths without
symlink ancestors, including for temporary directories on macOS.

## Select the execution treatment

Add both fields to the existing suite sandbox configuration:

```toml
[sandbox]
kind = "repair-v1"
runtime_image = "sha256:<immutable-runtime-image-id>"
context_archive = "results/context-v1.tar"
context_sha256 = "<archive-sha256>"
```

Relative archive paths resolve from the suite project root. The public suite
manifest records the digest, not the local archive path. The local Harbor job
configuration retains that path. Transfer the exact archive to the execution
host, update its local locator if necessary, and compile there. Changing the
capture changes the treatment and invalidates admission. Omission retains the
existing bare-context behavior.

Before starting the harness, the environment validates the archive and copies
its bytes into fresh container storage:

| Archive prefix | Container destination |
| --- | --- |
| `codex/` | `/tmp/codex-home/` |
| `resources/` | `/home/solver/context/resources/` |

There are no live host mounts or directory discovery from the operator's home.
The bounded solver home permits helper execution with `nosuid,nodev`, as solver
scratch already does. The broker's private storage remains non-executable.
The stock runtime remains free of personal context. Copies are solver-readable
and writable within the trial; later host edits cannot enter a running trial.
The original archive and its digest remain the reference for initial content.
`agent/context.json` records staged file hashes. Treat this solver-visible log
as diagnostic evidence, not independent proof of instruction loading.

Qualification passes the selected archive through every model's real CLI
tool-loop control. The fake provider captures the actual first request: checks
require the complete global guidance and skill paths in its instructions,
project guidance when present, and successful tool reads of every bundled file.
The receipt also records the native base-instruction hash. Authenticated controls
check raw session instruction messages; assistant claims do not count as loading.
Discovery is not proof that a model consulted a skill during a scored task.

## Export a true pre-fix checkout

```bash
python -m obench.frozen_context checkout /path/to/repository \
  --commit <full-pre-fix-commit-id> --exclusions results/exclusions.json \
  --output results/checkout-v1.tar
python -m obench.frozen_context unpack results/checkout-v1.tar \
  --kind checkout --sha256 <returned-sha256> \
  --destination results/new-task/environment/app
```

The destination must not exist. `exclusions.json` maps exact paths or directory
prefixes to review reasons. Every exclusion must match. Export reads pinned Git
objects with replacement refs disabled: uncommitted files, untracked files,
checkout filters and original history do not enter the archive. Symlinks and
submodules require reviewed exclusions; their targets are not followed.

Start with the full historical tree, retaining source, ordinary comments, docs,
tests, configuration and project `AGENTS.md`. Review hook/config discovery files
and answer-bearing or private assets explicitly. Keep commit/exclusion provenance
outside `environment/app` unless it is intended solver input. Do not overlay
post-fix tests, solutions or documentation into the candidate checkout.

Materialize this archive into a new self-contained Harbor task, select the
existing oracle, advance the case revision, and reseal the task. Document any
added `DEVELOPMENT.md`, command adaptations and actual provisioned dependencies.
Reconcile the oracle's caller interface with the exact historical source before
reusing it: a reconstructed slice may already contain API additions that the
historical checkout lacks. State any required interface extensions in the new
case's instruction and expose synthetic public compatibility examples. This is
an explicit case revision, not a reason to copy post-fix repository tests or
source into the original archive. The [Dojo contract supplement](../../benchmarks/fixtures/dojo-session-contract/CONTRACT.md)
illustrates native developer messages, historical fixture formats and the
mode-aware budget interface. Keep private full checkouts outside tracked paths.
If an untouched historical test fails because of a container timing assumption,
reproduce it outside the repair sandbox before adapting it. Preserve the original
archive; record the exact test-only change and before/after hashes in the new
case's provenance. Keep assertions intact and verify the adapted test through the
actual harness. A longer test shutdown guard is separate from the model's trial
deadline and must not silently extend that deadline.
The runtime preserves executable files and initializes one fresh synthetic Git
commit without remotes. Reusing an archive across oracle revisions does not
require another source export.

Before admission, prove untouched failure and reference-repair success in the
trusted grader, then exercise edit/test/typecheck/diff/cleanup through the real
harness. Public tests may correctly fail on the broken baseline: a narrowly
selected passing workflow check proves tool availability, not task correctness.
A full checkout does not imply all optional frontend, browser or service
dependencies are installed. Qualify the commands relevant to the repair and
state other limitations.

## Evidence and publication

Keep personal archives, provenance, original Git locators and captured requests
under ignored private storage. Preserve old archives and results. Track generic
code and synthetic controls publicly. The credential-free CI control is:

```bash
python scripts/local/verify_frozen_context.py \
  --runtime-image <digest> --output results/frozen-context-proof
```

It proves global/project loading, skill discovery, resource reads and executable
helpers using the pinned real Codex CLI with a fake, offline provider. It does
not perform live inference. Renew normal campaign admission on the execution
host before a scored run. Historical minimal-slice results and full-checkout,
captured-context results are different treatments; do not pool them.
