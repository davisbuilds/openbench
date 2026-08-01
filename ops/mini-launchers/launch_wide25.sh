#!/usr/bin/env bash
# Re-run laguna + deepseek under corrected model limits, both on this host.
# Preconditions checked up front: a launch missing a key burns every cell's
# retry budget in seconds on SETUP-NEEDED, which the queue records as infra.
set -uo pipefail
cd ~/dev/openbench
set -a; source ~/.openbench/keys.env 2>/dev/null; source ~/.config/secrets/secrets.env 2>/dev/null; set +a
for v in OPENROUTER_API_KEY DEEPSEEK_API_KEY; do
  eval "val=\${$v:-}"
  [ -z "$val" ] && { echo "ABORT: $v not set" >&2; exit 1; }
done
python3 -c "
import sys
from obench.run import host_version_drift
d = host_version_drift(['pi'])
if d:
    print('ABORT: host CLI drifts from the pin:', d, file=sys.stderr); sys.exit(1)
print('precondition OK: keys present, pi matches the pin')
" || exit 1
exec python3 -m obench.matrix_queue --spec wide25.toml
