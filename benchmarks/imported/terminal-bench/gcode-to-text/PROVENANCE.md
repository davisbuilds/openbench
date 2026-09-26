# Provenance

- **Upstream project**: Terminal-Bench (https://github.com/laude-institute/terminal-bench)
- **Upstream task**: `gcode-to-text` (original-tasks/gcode-to-text)
- **Upstream commit**: `1a6ffa9674b571da0ed040c470cb40c4d85f9b9b`
- **License**: Apache-2.0 (see the repository LICENSE; the upstream canary string is intentionally omitted from imported files)
- **Upstream difficulty**: `medium`

## Modifications made during conversion

- `instruction.md`: upstream prompt adapted so `text.gcode` and `out.txt` are relative to the working directory instead of `/app/...`.
- `workspace/text.gcode`: decompressed from upstream `text.gcode.gz`, matching the upstream Dockerfile's `gzip -d /app/text.gcode.gz` setup.
- `checker_data/text.gcode`: checker-owned copy of the input artifact.
- `checker_data/input_hashes.json`: SHA-256 hash of `checker_data/text.gcode`; the checker enforces the same hash for workspace `text.gcode`, closing the single-answer tampered-input hardcoding risk flagged in the scope report.
- `checker_data/expected_flag.txt`: decoded expected output moved out of the workspace.
- `checker.sh` + `checker_data/run_checks.py`: pure-stdlib replacement for upstream pytest tests. It validates input integrity and compares `out.txt` to the checker-owned expected text without printing the expected string on failure.
- `solution/out.txt`: solved deliverable containing the upstream expected output.

## Hardening notes

- No wall-clock timing assertions are used.
- The checker uses only Python standard library modules.
- The expected output is not present in `workspace/` or `instruction.md`; it lives only in checker-owned data and the non-agent-visible `solution/` overlay used by validation.
