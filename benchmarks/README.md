# Benchmark collections

| Collection | Format | Purpose |
| --- | --- | --- |
| `core/` | Legacy instruction/checker/workspace | Portable core tasks |
| `imported/` | Legacy instruction/checker/workspace | Imported Exercism and Terminal-Bench collections |
| `local/` | Legacy instruction/checker/workspace | Fork-owned PR repairs and controls |
| `candidates/` | Legacy instruction/checker/workspace | Candidates not yet admitted |
| `harbor/core/` | Native Harbor | Canonical suite tasks |
| `harbor/local/` | Native Harbor | Isolated fork-owned repair tasks |

Use `obench run` for Harbor suites. `obench validate` discovers the legacy core,
imported, and local tiers; use an explicit `--tasks-dir` for a particular tier.
A task's presence here does not establish difficulty or authorize a model run.
See [extra-hard repair admission](../docs/project/EXTRA_HARD_REPAIRS.md).

Snapshot source and reference bytes are frozen inputs. Keep development tools
outside individual task directories so housekeeping cannot silently change their
content identity. New task treatments require new identities and controls.
