# AgentMonitor #123 oracle controls

Development controls for [AgentMonitor #123](https://github.com/davisbuilds/agentmonitor/pull/123).
This is not a selectable benchmark task and has no model-difficulty label.

## Inputs and authority

`SOURCE_MANIFEST.json` pins the minimal dependency closure before both repair
rounds, the historical partial schema, and the final schema overlay. Other
source files stay at the baseline revision. Snapshot bytes are unchanged.
`FIXTURE_MANIFEST.json` records the reference capture and normalized SQL fixture:
tables precede their indexes/triggers and SQLite regenerates FTS shadow tables.

The existing `repair_worker` creates a disposable networkless, unprivileged
container with no host mounts, a read-only root and bounded scratch space.
Only captured source, the public observation driver and fixture operations enter
it. `oracle.py`, expected scores and reference controls stay on the host. No
credentials, model calls, package downloads or daily database access occur.
Host-side code checks resulting observations. The runner accepts only the exact
manifest inventory and bytes plus the repository-defined control mutations;
there is no submitted-source or arbitrary-patch input. Unexpected files and
edits to the snapshots are rejected before worker creation.

This is a fixed-control experiment, not an adversarial grading boundary. The
observed Node process supplies scheduling events, connection state and read
results over stdout. Editable code in that same process could forge those
events; a private file descriptor alone would not isolate it. Database snapshots
are separate observations, but do not independently prove all those fields.
Do not feed model submissions to this driver or register it as a trusted oracle.
Promotion requires externally owned observations and controls demonstrating
that forged events cannot earn credit or fabricate a schedule.

## Concurrency scheduling

The driver records a serial run's database-read count, then repeats from fresh
state while pausing the first process after each ordinal read. It instruments
read-only statement execution and the database's pragma interface, without
matching SQL text, requiring a specific transaction method, or prescribing where
a repair acquires ownership. Iterated rows are observation boundaries too.

Before the second process starts, an independent SQLite connection tests the write
slot. When the first process owns it, the controller releases that process to
avoid creating a test-induced deadlock. When it is free, the second process may
finish before the first resumes: the controller waits for its completion event
using the ordinary worker deadline, and the receipt records that actual ordering.
There is no shorter grace cutoff. Exceeding the worker deadline aborts the
control run; it never fabricates a completed interleaving or a repair score.
Every schedule requires both processes to finish correctly and preserve state.
Known-buggy controls must witness a second completion before the first release.

This is a finite read-boundary sweep, not exhaustive verification of all possible
process schedules. The 64-read development budget fails explicitly. Alternative
synchronization strategies must pass before task promotion; no particular
checkpoint count earns or loses points. SQL executed entirely in additional
candidate processes is outside this instrumentation's coverage.
The fixed controls use SQLite ownership or optimistic retry; scheduling repairs
that hold external mutexes is not supported by this development controller.

## Behavioral obligations

- Structural upgrade from a genuinely old table: all required tables/columns,
  retained sentinel content, current version and clean integrity check.
- Non-idempotent token correction exactly once across two processes; preserve
  unrelated provider rows and check reopening changes nothing.
- Legacy export foreign keys removed with export rows and connection foreign-key
  mode preserved.
- Current-state reads complete while another connection retains its WAL write
  transaction. Old-state reads perform the full upgrade.
- An injected error during the second row correction rolls back the first row
  and version marker; retry applies the correction once.

Controls include the historical baseline/partial/reference, optimistic structural
retry, alternate SQL/transaction APIs, overbroad initialization on reads,
non-atomic migration, column-only conflict recovery, and a no-op initializer.
Additional columns are allowed; removing required schema or preserved content
fails.

## Run

From a clean pushed checkout on the execution host, inside tmux/caffeinate:

```sh
python scripts/local/verify_am123_oracle.py \
  --runtime-image sha256:REPLACE_WITH_PINNED_IMAGE \
  --output results/am123-controls/REPLACE_WITH_FRESH_ATTEMPT
```

Use the pinned Harbor interpreter and runtime from the repair integration lane.
`--variant NAME` restricts a diagnostic control; `--capture-fixture` regenerates
reference fixture artifacts in a fresh output directory for explicit review.
Per-case receipts retain schedules, worker outcomes, schema observations and
source/driver/oracle/image identities. Preserve failed attempts. Rerun controls
when those inputs change; never pool development receipts with model trials.

Before packaging, replace the same-process event trust described above, add the
public task contract, seal its source/oracle identity, and prove the registered
verifier through Harbor and canonical suite import.
Only then run fresh model screening under the extra-hard repair spec.

## Development finding

The first full read-boundary sweep also exposed an FTS trigger-creation race
that the older column-specific prototype never scheduled. The initial optimistic
repair handled duplicate columns but failed on an already-created trigger. That
version remains a negative control; the alternative must handle both conflicts.
This establishes an additional repair obligation, not a model-difficulty claim.
