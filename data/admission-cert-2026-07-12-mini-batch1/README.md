# Determinism certification — import batch 1, Mac mini docker mode (2026-07-12)

Node: Matthews-Mac-mini.local | exec: `docker run --cpus 4` | image `sha256:a6fc4415be102e...` (CERT_STAMP.txt).
Config: 20 solution + 10 workspace runs per task, stress=6.

| Task | Determinism | Sol wall med (s) |
|---|---|---|
| db-wal-recovery | PASS | 0.26 |
| extract-elf | PASS | 0.28 |
| gcode-to-text | PASS | 0.25 |
| raman-fitting | PASS | 0.24 |

All four are certified for mini docker-mode matrices.
