# Dojo evidence repair, isolated grading revision 3

This local task requires the isolated Harbor environment and
`obench.sandbox_grading:RepairVerifier`. The ordinary shell verifier deliberately
fails closed. Hidden oracle code, expected answers and authoritative reward files
are never supplied to the candidate container or candidate worker.

Only the existing `scripts/profiles/*.py` source files are submitted. The stopped
solver's validated source reaches a fresh fixed-runtime worker; the trusted
external grader sends function inputs and compares serialized outputs. The
worker has no external network or host mounts, runs unprivileged with dropped
capabilities, and has a read-only root plus disposable temporary storage.

Candidate-caused failures (invalid submitted paths, import exceptions, malformed
worker output, output flooding, and execution timeout) receive a trusted zero
and a reason in the grading receipt. They stay in the evaluation denominator.
Missing images, Docker failures, or unproven solver shutdown remain infrastructure
errors; they are not silently converted into model failures.

## Task binding

This task uses **digest scheme 3**, distinct from native task schemes 1 and 2.
Its canonical manifest contains:

- Parsed `task.toml`, excluding only its self-referential
  `metadata.openbench_task_content_digest` field.
- SHA-256 of every other regular task file, including build-context dotfiles.
- SHA-256 of the actual external grading module and its public worker entrypoint.

Symlinks and special task files are rejected. The trusted verifier checks the
binding before freezing source and again before starting the candidate worker,
and records the manifest in its trusted grading receipt. Changing oracle code,
source, instructions, environment policy, Dockerfile or dependency metadata
requires a new binding. No digest value is embedded in this document.

Compute the binding after all edits:

```sh
python3 -m obench.sandbox_grading harbor-tasks-local/dojo-evidence-pr60-v3
```

Write that scheme and hash to the task metadata, then check it:

```sh
python3 -m obench.sandbox_grading harbor-tasks-local/dojo-evidence-pr60-v3 --check
```

Acceptance of scheme 3 by the isolated repair result-import path does not extend
legacy/native publication digest support. Offline boundary controls are separate
from live harness/model admission and do not establish model difficulty.
