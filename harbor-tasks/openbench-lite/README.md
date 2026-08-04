# OpenBench Lite Harbor tasks

These eight directories are the canonical Harbor-native forms of OpenBench's
core coding tasks. Each task maps one-to-one to the byte-stable legacy input at
the pinned source commit below; normal Harbor execution uses these committed
directories directly and does not export or re-import `tasks/`.

Source commit: `802014700f6b3c62eddc1a406e3062a438ce572f`

| Harbor task | Legacy source |
| --- | --- |
| `add-feature` | `tasks/add-feature` |
| `build-a-cli` | `tasks/build-a-cli` |
| `fix-failing-test` | `tasks/fix-failing-test` |
| `make-ci-green` | `tasks/make-ci-green` |
| `make-it-run` | `tasks/make-it-run` |
| `misleading-error` | `tasks/misleading-error` |
| `taskflow` | `tasks/taskflow` |
| `webcore` | `tasks/webcore` |

Each task follows Harbor schema 1.4:

- `environment/app/` is the starting workspace at `/app`.
- `tests/` contains the post-agent verifier and checker-owned data.
- `solution/` contains the oracle overlay.
- `task.toml` records the source path, source commit, and OpenBench content
  digest.
