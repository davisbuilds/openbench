# Provenance

- **Upstream project**: Terminal-Bench (https://github.com/laude-institute/terminal-bench)
- **Upstream task**: `db-wal-recovery` (original-tasks/db-wal-recovery)
- **Upstream commit**: `1a6ffa9674b571da0ed040c470cb40c4d85f9b9b`
- **License**: Apache-2.0 (see the repository LICENSE; the upstream canary string is intentionally omitted from imported files)
- **Upstream difficulty**: `medium`

## Modifications made during conversion

- `instruction.md`: upstream prompt adapted so `main.db`, `main.db-wal`, and `recovered.json` are relative to the working directory instead of `/app/...`.
- `workspace/main.db` and `workspace/main.db-wal`: copied from upstream `main.db` and `main.db-wal.encrypted` (renamed to the live WAL filename, matching the upstream Docker image).
- `checker.sh` + `checker_data/run_checks.py`: pure-stdlib replacement for upstream pytest tests. It validates JSON shape, sorted unique ids, exact row content against checker-owned `checker_data/expected_rows.json`, and proves the submitted `main.db-wal` is a valid repaired WAL by applying it to checker-owned `checker_data/main.db` (the original base DB) and reading the same recovered rows with SQLite.
- `checker_data/expected_rows.json`: expected recovered rows generated from the upstream reference solution (XOR-decrypt WAL with key `0x42`, then read `SELECT id, name, value FROM items ORDER BY id`). These expected values are not present in the workspace.
- `checker_data/input_hashes.json`: SHA-256 hashes of the starting `main.db` and encrypted `main.db-wal`, retained as checker-owned provenance for the oracle inputs.
- `checker_data/main.db`: checker-owned copy of the original base database used to verify that the submitted repaired WAL, rather than a reconstructed `main.db`, recovers the expected rows.
- `solution/recovered.json`: solved JSON deliverable produced by the upstream reference recovery flow.
- `solution/main.db-wal`: XOR-decrypted WAL produced by the upstream reference recovery flow, included so validation proves the stated WAL-repair deliverable as well as the JSON extraction.

## Hardening notes

- No wall-clock timing assertions are used.
- The checker uses only Python standard library modules (`json`, `pathlib`, `os`, `sys`).
- Expected recovered rows live only under `checker_data/`, not in `workspace/` or `instruction.md`.
