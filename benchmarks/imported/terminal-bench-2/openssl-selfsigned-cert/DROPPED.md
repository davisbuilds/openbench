# DROPPED 2026-07-13

Reason: format limitation, not task quality. The upstream test asserts the
private key file mode is <= 0600. Our task format overlays solution/ files
from a git checkout, and git cannot store non-executable file modes — a fresh
clone materializes server.key as 0644, so the golden solution fails polarity
on any machine other than the one that authored it.

The permission assertion is the point of the task (checker-owned oracle), so
weakening the checker is not acceptable.

Re-admit when the task format grows either:
- a solution-apply hook (run solve.sh to materialize the oracle instead of
  file overlay), or
- git-state packaging that preserves modes (same primitive needed for
  repo-import tasks).

Everything else about the import (derived pinned image, --network none,
20/20 determinism in the authoring worktree) was sound.
