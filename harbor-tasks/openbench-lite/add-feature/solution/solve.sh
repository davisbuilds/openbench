#!/usr/bin/env bash
# OpenBench → Harbor oracle: copy solution tree onto the workspace (cwd).
set -euo pipefail

SOLUTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Harbor copies solution/ to /solution and runs solve.sh from the workdir.
# Overlay every solution file except this generated runner itself.
while IFS= read -r -d '' src; do
  rel="${src#"$SOLUTION_DIR"/}"
  case "$rel" in
    solve.sh) continue ;;
  esac
  dest="./$rel"
  mkdir -p "$(dirname "$dest")"
  cp -f "$src" "$dest"
done < <(find "$SOLUTION_DIR" -type f -print0)

echo "openbench harbor oracle: solution overlaid"
