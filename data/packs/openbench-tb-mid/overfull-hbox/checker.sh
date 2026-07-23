#!/bin/bash
set -uo pipefail
IMAGE=openbench-tb2-overfull-hbox@sha256:b30720af09e1818e4c906be3414edad593d3d271992c5257532d4f06f2ec1e05
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
    if [ "${OPENBENCH_SOLUTION_OVERLAY:-}" = "1" ]; then bash /openbench-checker/oracle/solve.sh >/tmp/oracle.log 2>&1 || { cat /tmp/oracle.log >&2; exit 2; }; fi
    /opt/openbench-venv/bin/pytest -q -p no:cacheprovider /tests/test_outputs.py
  '
rc=$?
if [ $rc -eq 0 ]; then echo "SCORE: 1.0"; exit 0; fi
exit $rc
