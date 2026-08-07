# Create the exact TextEdit file

Use only the provided computer-use tools and TextEdit to create and save one
file at the run-scoped path provided in `run-context.json` (also available as
`OPENBENCH_NATIVE_OUTPUT_PATH` when the native runner invokes the verifier).

The file must contain exactly these two lines, including the final newline:

```text
OpenBench computer-use v0
café
```

Do not create any other files in `artifacts/`, and do not write the target file
through shell or filesystem tools. The verifier checks the resolved path and
exact UTF-8 bytes.
