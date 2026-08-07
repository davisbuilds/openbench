# TextEdit exact-file contract

The native runner creates a fresh workspace and supplies the target path in
`run-context.json` and `OPENBENCH_NATIVE_OUTPUT_PATH`. The verifier requires
both sources to resolve to the same regular file inside that workspace, checks
the exact UTF-8 bytes (including the final newline), rejects symlinks, and
rejects every extra entry under `artifacts/`.
