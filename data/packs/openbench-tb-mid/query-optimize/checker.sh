#!/bin/bash
set -uo pipefail
# Image comes from the runner (BENCH_TASK_IMAGE) so the checker runs in the
# SAME image as the agent. The literal below is a hand-run fallback only --
# never a second source of truth: task.toml/images.json own the digest.
IMAGE="${BENCH_TASK_IMAGE:-openbench-tb2-query-optimize@sha256:9e847e4af966d76e41363651967f2523e469bd8a8e517b5364a9f79fe95d4134}"
WORKDIR=/app
if [ ! -f .openbench-image-hydrated ]; then
  cid=$(docker create "$IMAGE") || exit 2
  trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
  docker cp "$cid:$WORKDIR/." "$PWD" || exit 2
  docker rm -f "$cid" >/dev/null; trap - EXIT
fi
docker run --rm --network none -e OPENBENCH_SOLUTION_OVERLAY \
  -v "$PWD:$WORKDIR" -v "$TASK_DIR/checker_data:/openbench-checker:ro" -v "$TASK_DIR/checker_data/tests:/tests:ro" \
  -w "$WORKDIR" "$IMAGE" bash -lc '
    set -o pipefail
    if [ "${OPENBENCH_SOLUTION_OVERLAY:-}" != "1" ] && [ ! -f /app/sol.sql ]; then exit 1; fi
    if [ "${OPENBENCH_SOLUTION_OVERLAY:-}" = "1" ]; then bash /openbench-checker/oracle/solve.sh >/tmp/oracle.log 2>&1 || { cat /tmp/oracle.log >&2; exit 2; }; fi
    /opt/openbench-venv/bin/pytest -q -p no:cacheprovider /tests/test_outputs.py
  '
rc=$?
if [ $rc -eq 0 ]; then echo "SCORE: 1.0"; exit 0; fi
exit $rc
