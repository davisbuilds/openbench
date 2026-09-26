# Provenance

- **Upstream project**: Terminal-Bench (https://github.com/laude-institute/terminal-bench)
- **Upstream task**: `raman-fitting` (original-tasks/raman-fitting)
- **Upstream commit**: `1a6ffa9674b571da0ed040c470cb40c4d85f9b9b`
- **License**: Apache-2.0 (see the repository LICENSE; the upstream canary string is intentionally omitted from imported files)
- **Upstream difficulty**: `medium`

## Modifications made during conversion

- `instruction.md`: upstream prompt adapted so `graphene.dat` and `results.json` are relative to the working directory instead of `/app/...`.
- `workspace/graphene.dat`: copied verbatim from upstream `task-deps/graphene.dat`.
- `checker.sh` + `checker_data/run_checks.py`: pure-stdlib replacement for upstream pytest tests. It validates JSON shape and compares submitted G/2D Lorentzian parameters against checker-owned expected values and upstream tolerances.
- `checker_data/expected_params.json`: expected fit parameters and tolerances moved out of agent-visible files.
- `checker_data/input_hashes.json`: SHA-256 hash of the input spectrum. The checker enforces this hash so a modified spectrum cannot satisfy the oracle accidentally.
- `solution/results.json`: solved deliverable containing the upstream reference fit parameters.
- `REQUIREMENTS.txt`: documents non-stdlib packages used by the upstream reference fitting approach (`numpy`, `scipy`). The checker does not import them.

## Hardening notes

- No wall-clock timing assertions are used.
- The checker uses only Python standard library modules.
- Expected fit constants live only under `checker_data/`, not in `workspace/` or `instruction.md`.
