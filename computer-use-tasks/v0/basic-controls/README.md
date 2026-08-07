# Basic controls contract

The pinned fixture writes schema-v1 JSON to
`COMPUTER_USE_FIXTURE_STATE_PATH`. The native runner maps that fixture-owned
file to `artifacts/basic-controls-state.json` for checking. The verifier
requires the exact five-field schema and values: toggle on, honest counter 2,
and keystroke echo `openbench-42`.
