# Computer-Use Bench v0

This task root defines three deterministic native macOS computer-use tasks.
Each task keeps the familiar OpenBench envelope (`task.toml`,
`instruction.md`, `workspace/`, `solution/`, and `checker.sh`) and adds an
`openbench.native-task.v1` sidecar for host-only requirements.

These are not Harbor-executable tasks. Harbor's container runtime cannot drive
macOS applications. A native runner must satisfy each sidecar, provide the
declared run-scoped paths, and keep runner-owned evidence outside the agent's
writable workspace. The shell checkers are the sole pass/fail judges; no LLM
judgment or screenshot is authoritative.

Fixture contracts are pinned to `computer-use-mcp` commits:

- `2c5cc162e58f6486505c8c5fe87fd76980d0e6b9` for basic controls.
- `1e7988c157c880a4588cdd593e42a5f86443c307` for background controls.

Run offline polarity validation with:

```bash
obench validate --tasks-dir computer-use-tasks/v0
python3 -m unittest obench.tests.test_computer_use_tasks -v
```

The `diagnostics/v1/` fixtures lock controlled negative classifications. They
are diagnostic examples and never contribute to the three primary pass
criteria.
