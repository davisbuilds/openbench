# Task fidelity for review and repair evaluations

A valid package, passing checker polarity and equal treatment inputs do not
establish that a task represents the workflow being measured. Inspect the
actual exported workspace and execution route before model trials. This guide
applies to private packages and checkout-based tasks; it does not replace
[runtime admission](benchmark-operations.md) or [quality validation](repair-validation.md).

## Preserve the job the agent would actually receive

Pin the review base and candidate separately from later fixes. For a full PR
review, retain the entire proposed diff, ordinary repository instructions,
relevant callers, domain contracts, existing tests, manifests and lockfiles.
Do not strip AGENTS.md simply because it contains agent instructions. Remove
only material that contaminates the selected treatment or exposes hidden
answers, and record each exclusion and its effect on interpretation. A selected
file or subsystem review is legitimate when that is the intended task; label
it as such instead of claiming full-review fidelity.

Distinguish tests already present at the review point from newly authored hidden
regressions and later repair tests. The former are ordinary context. Preserve
the latter outside the solver unless the task explicitly calls for them.
Do not manufacture difficulty by withholding necessary documentation or tools.
Keep original review discussions, later fixes, unrelated repository history,
other trials and private evaluator material inaccessible to the solver.

For review work, check actual Git commands. A clean repository initialized at
the proposed snapshot has an empty native diff even if a separate patch exists.
The repair runtime can reconstruct two synthetic commits from a sealed full
binary patch: put `{"schema":1,"patch":"review.patch"}` in
`.openbench-review.json` beside the candidate source and `review.patch`.
It exposes `review-base` and `review-candidate` (`HEAD`), with no original
commit messages, authors, remotes or later objects. Use `git diff review-base
HEAD`. Invalid or inapplicable patches fail initialization. Packages still
cannot supply `.git`. Verify the reconstructed base and diff against the
selected source; synthetic Git is not proof that extraction was faithful.

Frozen packages accept regular files only. If an in-tree instruction alias is
materialized, record that normalization. Reject unsupported links rather than
silently omitting them. A change to link semantics needs an appropriate runtime,
not a regular-file approximation.

## Make the normal tools work

Qualify the actual task's inspection, test and build commands as the solver UID,
offline, in the same image and writable/read-only layout used for trials. A
compiler version or a host-side passing oracle is insufficient. Provision
lockfile-compatible dependencies before launch; record toolchain and dependency
identities. Keep task source, hidden verifiers and repairs out of dependency
images. Report unavailable services and relevant failing baseline tests, rather
than attributing environmental failures to the model.

Check the project's supported OS, not just its language version. Cross-compiling
macOS-only packages on Linux establishes buildability, not executable tests or
runtime parity. Preserve historical source instead of patching it to fit the
harness. Explain the limitation identically to both arms and classify the case
as platform-limited diagnostic material until a suitable isolated runtime exists.
Do not pool it with native-workflow cases as evidence of representative review
performance.

Tests and builds may create untracked artifacts. Full-review packages using the
sealed descriptor above permit added regular files while protecting every
supplied file against changes/removal. Legacy packages retain strict scope
checking. Archive size/link limits still apply; use temporary directories for
large builds. Artifact schema validity is transport correctness, not review
quality.

## Isolate the intended difference

For a skill comparison, hold the ordinary harness, prompt, repository context,
model/reasoning, tools, limits and dependency image constant. Vary only the
selected skill. Do not coach the baseline with the skill's procedure or weaken
its normal abilities. Native review mode versus an ordinary agent is a distinct
harness comparison. Capture the actual loaded instructions and skill reads;
availability and consultation answer different questions. Retain trials that
did not consult an available skill instead of selecting successful use afterward.

Choose budgets for the complete job; carry over neither a tiny-fixture time cap
nor a file-hunt prompt without checking suitability. Preserve failures, retries,
capacity errors and interrupted attempts, and distinguish them from valid
misses. Freeze treatment choices before examining scored outcomes.

## Grade the review, not agreement with one historical comment

Historical accepted findings are grounded targets, not an exhaustive answer key.
Use arm-blind semantic adjudication for every report, including empty reports
and lexical matches. Accept additional supported defects; deduplicate equivalent
findings; assess trigger, consequence, location and severity. Keyword matches
can reward a wrong causal explanation. Unrecognized findings are pending review,
not automatically false positives.

Include appropriate clean review changes when assessing precision. Passing
behavioral countercontrols or two whitespace variants of an empty report do not
measure false-positive rate. Fixed variants of the same case are useful paired
controls, but are not independent samples and are not automatically globally
bug-free. Keep development exposure separate from holdout claims. A few selected
bug-containing PRs can diagnose a skill; they cannot establish representative
performance across a project's review workload.
