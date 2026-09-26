# AgentMonitor #106, isolated revision 3

This packages the corrected revision-2 behavioral contract in the confined repair
lane. It is a new execution treatment, not a new difficulty claim. The source
snapshot and corrected requirements come from `tasks-local/am-benchmark-pr106-v2`.
See that task's provenance and source manifest for public source revisions and
licensing. Candidate source under `src/` may change, add helpers, or remove files;
workspace paths and archive types remain validated. Build metadata is fixed.

The trusted built-in oracle is `agentmonitor-benchmark-v2`, using public
`am-benchmark-observations-v1` worker operations and scheme-4 task binding. Host
comparisons cover identity, real startup upgrades, task/trial coverage, and usage
separation guards. Solutions and expected values are never staged to the solver
or worker. Missing or malformed candidate behavior earns zero; broken confinement
or runtime evidence fails the trial as infrastructure.

No model difficulty admission is claimed by this packaging. Untouched, reference,
partial, and alternative-valid controls must pass in the actual image before use.
