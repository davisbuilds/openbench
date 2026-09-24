# CI and local runtime checks

Our trunk keeps two main validation lanes: portable offline contracts
and the pinned Harbor/Docker integration. Neither makes a live model request or
uses subscription credentials. Authenticated calibration remains a separate,
explicit run against an exact commit and immutable runtime.

| Workflow | Coverage |
|---|---|
| CI | Python 3.11/3.13 unit tests; core, imported offline, and portable fork-local checker polarity |
| Harbor Integration | Real pinned Codex trajectory conversion; Docker grading controls, confinement, completion/timeout classification, artifact import, and bounded log export |
| Publication Hygiene | Tracked public content and detector controls |
| Workflow Security Audit | Pinned zizmor audit and dependency-lock drift |
| Community submissions | Reverification of changed published bundles |
| CLI version check | Advisory compatibility-image update issues; never upgrades a frozen runtime or host |

Keep the existing `test (3.11)`, `test (3.13)`, and `check` job names stable when
changing workflows. If required-check configuration is introduced or changed,
coordinate its names with the jobs. Integration exercises the actual Harbor
Trial lifecycle; whole-suite assembly/import also has offline contract tests.
The integration lane does not establish model accuracy or authenticated provider
connectivity. Hosted Linux coverage complements local Docker-on-macOS checks.

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
