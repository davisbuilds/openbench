# CI and local runtime checks

Our trunk keeps two main validation lanes: portable offline contracts
and the pinned Harbor/Docker integration. Neither makes a live model request or
uses subscription credentials. Authenticated calibration remains a separate,
explicit run against an exact commit and immutable runtime.

| Workflow | Coverage |
|---|---|
| CI | Python 3.11/3.13 unit tests; core, imported offline, and portable fork-local checker polarity |
| Harbor Integration | Real pinned Codex trajectory conversion; gateway stream lifecycle in the runtime Python, Docker grading controls, confinement, completion/timeout classification, artifact import, and bounded log export |
| Publication Hygiene | Tracked public content and detector controls |
| Workflow Security Audit | Pinned zizmor audit and dependency-lock drift |
| Community submissions | Reverification of changed published bundles |
| CLI version check | Advisory compatibility-image update issues; never upgrades a frozen runtime or host |

Keep the existing `test (3.11)`, `test (3.13)`, and `check` job names stable when
changing workflows. If required-check configuration is introduced or changed,
coordinate its names with the jobs. Integration exercises the actual Harbor
Trial lifecycle; whole-suite assembly/import also has offline contract tests.
Harbor integration uses five independent hosted runners: `repair-grading`
(trajectory conversion, stream lifecycle, grading oracles, quality admission,
sandbox boundaries, timeouts, artifact import and log export), `repair-tools`
(Codex tool loops, developer workflows and frozen context), `concurrency`,
`browser` (interactive review, sandbox boundaries, parallel lifecycles, grading
import and package admission) and `browser-quality` (screenshot transport, then
the browser controls that consume its receipt). Each installs the same frozen
graph, builds its own runtime, preserves all existing checks, and verifies
cleanup. The `sandbox` aggregate remains the gate and succeeds only when every
selected lane succeeds; a failed, cancelled, or skipped matrix cannot pass it.
Pull requests may deselect `browser-quality` (see below). Summaries use
lane-specific artifact names. Each lane has a 20-minute bound, with a five-minute
`classify` job ahead of the lanes and a two-minute aggregate job.

The previous serial job took 24m43s. The three-lane split that followed took
16-18 minutes, bound by a browser lane of about 17 minutes. Its browser-quality
step alone takes about 8-9 minutes: it grades every control once for admission
and again for fresh replay, serially, through the production validator. Lanes
are now balanced on measured step times, so `browser-quality` (about 12 minutes)
sets the critical path and the other lanes finish well inside it. Building the
runtime once and sharing the image would not shorten that path: the browser
image holds about 1 GB of compressed layers in a local build, and a shared image
needs a separate build, export and upload before any lane starts, whereas the
two-minute cold builds already overlap across runners.
Grading and fresh admission replay still run uncached.

Pull requests that change only `docs/`, root prose files (`README.md`,
`CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`,
`LICENSE`) or GitHub issue/PR templates skip Harbor integration; none of its
checks read those paths. Pushes to `main` always run it. CI is not filtered,
because unit tests read published site data under `docs/`. Branch protection
does not currently require any check. If `sandbox` becomes required, replace the
workflow-level filter with an in-workflow change detector: a skipped workflow
leaves a required check pending.

### browser-quality runs only when its inputs change

On pull requests, a `classify` job runs before the lanes and drops
`browser-quality` from the matrix when no changed file is one of its inputs. The
other four lanes always run. Pushes to `main`, manual dispatches and every other
event always include `browser-quality`. When it is skipped, `repair-tools`
(about 8 minutes) becomes the critical path.

`scripts/ci/browser_quality_inputs.py` derives the inputs from source each time;
there is no file list to maintain:

- every integration step the lane can run, which is any step not conditioned
  exactly on `matrix.lane == '<another lane>'`, contributes the repository
  paths it names: entry scripts, the activity-explorer task and the Harbor
  dependency locks;
- Python files are followed through their imports, with the same AST approach
  as `obench/evidence_identity.py`. Literal `obench.*` names count; reaching the
  adapter, Harbor-agent or oracle package includes every member. Bare imports
  resolve to `scripts/ci` and `scripts/local`;
- string literals in that closure add the tracked paths they name (runtime build
  contexts under `docker/`, Dockerfiles, package locks, task directories), files
  beside the module, and non-Python files under `obench/`;
- the workflow and the classifier are always inputs.

The input set is the union for the merge base, the current base, the PR head
and the merge commit the run tests. Deleting an input, or a dependency the base
branch gained after the PR branched, still runs the lane. It is deliberately coarse: an unused import or a path
in help text can make the lane run, never skip. The closure currently holds
about 114 files plus directories such as `obench/repair_oracles/` and
`docker/browser-runtime/`. Print it with:

```sh
python3 scripts/ci/browser_quality_inputs.py --list        # working tree
python3 scripts/ci/browser_quality_inputs.py --base BASE_SHA --head HEAD_SHA
```

The classifier fails closed. A non-SHA or unknown revision, shallow history, an
empty comparison, an unparseable source, or a workflow layout it does not
recognize (no step conditioned on `browser-quality`, or a lane step naming no
repository path) all select running the lane. The job's step summary records
the decision and its log holds the JSON report: changed files and matched
inputs. The `sandbox` gate accepts a missing `browser-quality` lane only when
`classify` succeeded and chose the skip for a pull request. To force the lane,
run the workflow manually (`gh workflow run harbor-integration.yml --ref
BRANCH`) or change one of its inputs. The decision is computed from pull request
code, as the workflow itself is; it is a cost control, not a trust boundary.

The integration lane does not establish model accuracy or authenticated provider
connectivity. Hosted Linux coverage complements local Docker-on-macOS checks.
The browser lane also tests frozen evaluation-package replay and solver export
boundaries with synthetic public fixtures. It neither accesses private scored
packages nor grants their campaign admission.

## Unit tests run one module per process

`python3 scripts/ci/unit_tests.py` is the unit-test command for CI and local
runs. It discovers the suite exactly as
`python3 -m unittest discover -s obench/tests` does, then runs each test module
in its own fresh interpreter from the repository root, several at a time. A
module therefore never shares process state (environment, working directory,
patched globals) with another module, which is stricter than the serial run.
The run fails if any module fails or reports a different number of tests than
discovery found in it, so a dropped or unimported module cannot pass silently.
Failing modules print their full log and a single-module reproduction command.

Workers default to `OBENCH_TEST_WORKERS`, else the CPU count. `--workers 0`
(or `OBENCH_TEST_WORKERS=0`) runs the serial reference command unchanged; use
it to rule the runner out when a failure appears only in parallel. The serial
suite spends most of its time waiting on subprocesses and timers (about 30% of
one core), so CI uses 8 workers on the hosted runner's 4 vCPUs. Known-slow
modules start first (`SLOW_FIRST` in the runner; order only, so a stale entry
changes wall time, never coverage). The floor is `test_sandbox_gateway`, about
30 seconds, which the runtime-qualification fingerprint hashes and so is not
split.

Parallel safety rests on an audit of the suite: servers bind ephemeral ports,
scratch files use `tempfile` or the per-call `.bench-tmp/` directories, the tmux
control uses a random session name, real Docker tests are environment-gated and
skipped in CI, and a full run writes nothing else in the checkout except
`__pycache__`. Keep new tests to that contract: no fixed ports, fixed paths
outside a temporary directory, or cleanup that matches other tests' resources
by name.

Hosted (4 vCPUs), the serial step took 246-288s. With the runner it took 81-85s
at 4 workers, 64-71s at 8 and 61-72s at 12; at 8 and above it is bound by
CPU-heavy modules (`test_site`, `test_evidence_identity`, about 25s each alone,
60-70s under contention). Twelve workers exposed a 0.2-second stall budget in
`test_stall_kill`, since widened; prefer fewer workers to retrying a timing
flake. On the Mac mini (10 cores): serial 240s, 8 workers 35-51s, 12-16
workers 26-31s.

## Workflow baseline

Use full action commit SHAs with verified release comments, read-only top-level
permissions, job-scoped exceptions, explicit runner versions, justified job
timeouts, and ref-scoped cancellation. Never interpolate untrusted event data
into shell programs. Dependency graphs are frozen; PRs never run on a personal
persistent machine. Dependabot maintains action pins; check version comments too.

The core jobs retain stdlib-only runtime dependencies. Their build backend is
hash-locked in `.github/requirements/build.txt`; editable installation disables
build isolation and dependency resolution. The Harbor integration graph is
separately pinned (including its `uv_build` backend) to our supported Harbor Git commit in
`.github/requirements/harbor.txt`. This optional graph does not become a package
runtime dependency.

To deliberately refresh the locks (use the workflow's pinned uv version):

```sh
uv pip compile --universal --python-version 3.11 --generate-hashes --no-header --no-annotate .github/requirements/build.in -o .github/requirements/build.txt
uv pip compile --universal --python-version 3.13 --no-header --no-annotate .github/requirements/harbor.in -o .github/requirements/harbor.txt
uv pip compile --universal --python-version 3.13 --generate-hashes --no-header --no-annotate .github/requirements/harbor-build.in -o .github/requirements/harbor-build.txt
python3 scripts/ci/check_locks.py
uvx zizmor@1.30.1 --offline .github/workflows/
```

Lock checking resolves against existing version constraints and fails on drift;
it never silently accepts an upgrade. Change the Harbor input and supported
runtime pin together. Tool versions embedded in shell commands, including uv and
zizmor, need deliberate maintenance because the Actions updater does not track
them.

## Runtime probes

The integration workflow documents the exact local commands. First install its
frozen Harbor graph and build a runtime with `scripts/local/build_repair_runtime.py`.
Use a fresh output directory for each probe. The deadline control substitutes
synthetic work at the model-call seam while retaining the production adapter,
Harbor Trial, real Docker environment, export, and verifier. It proves both
completion followed by slow cleanup and interruption of unfinished work.

Scripts clean their resources in normal and failure paths; the integration job
also checks for surviving sandbox resources. Hosted runners are ephemeral.
Captured transcripts and benchmark artifacts remain local-only; CI probes use
synthetic inputs and do not upload raw artifacts.

The browser lane also runs a wholly synthetic private-package campaign control:
actual Codex tool execution, archive-read denial, fresh quality admission, and
Harbor verifier/import checks for valid, malformed and forged-reward submissions.
No private evaluation assets or credentials enter hosted CI.
