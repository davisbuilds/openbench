# Provenance

- **Upstream project**: Terminal-Bench (https://github.com/laude-institute/terminal-bench)
- **Upstream task**: `extract-elf` (original-tasks/extract-elf)
- **Upstream commit**: `1a6ffa9674b571da0ed040c470cb40c4d85f9b9b`
- **License**: Apache-2.0 (see the repository LICENSE; the upstream canary string is intentionally omitted from imported files)
- **Upstream difficulty**: `medium`

## Modifications made during conversion

- `instruction.md`: upstream prompt adapted so `a.out`, `extract.js`, and `out.json` are relative to the working directory instead of `/app/...`.
- `workspace/a.out`: compiled from upstream `task-deps/hi.c` in a Linux GCC container, mirroring the upstream Dockerfile's `gcc /app/hi.c -o /app/a.out` step. The compiled ELF is shipped directly so the OpenBench task does not require a compiler.
- `checker_data/probe.out`: checker-owned ELF binary compiled from the upstream pytest probe C program. This replaces upstream's test-time `gcc` dependency. The expected memory map is derived from this binary at check time by `checker_data/run_checks.py`; no expected-map JSON is stored on disk.
- `checker_data/input_hashes.json`: SHA-256 hashes for both the visible `a.out` and checker-owned `probe.out`; the checker enforces them so mutated input artifacts cannot satisfy the oracle accidentally.
- `checker.sh` + `checker_data/run_checks.py`: pure-Python-stdlib checker that derives the reference map from checker-owned `probe.out`, stages that probe at an opaque temporary path, invokes `node --permission extract.js <staged-probe>` with a scrubbed environment and read access only to `extract.js` plus the staged probe, validates JSON, rejects unknown or incorrect included addresses, and requires at least 75% coverage of the derived expected map. The subprocess timeout is a generous 60-second safety bound, not a scoring cutoff.
- `solution/extract.js`: reference extractor adapted from upstream `solution.sh` into the final deliverable file.

## Requirements

- The checker uses Python standard library only, but the task itself requires `node` to run `extract.js`; the checker also uses Node permission flags (`--permission`, `--allow-fs-read`) to isolate the submitted extractor from checker-owned files. `REQUIREMENTS.txt` records `node>=22`. The default `openbench-harness` image is based on Node and already provides it.
- No Python packages, compiler, or pytest are required at grading time.
