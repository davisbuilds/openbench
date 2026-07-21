#!/usr/bin/env bash
set -uo pipefail
[ -f filter.py ] || { echo 'missing filter.py'; exit 1; }
t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
cat >"$t/clean.html" <<'EOF'
<!DOCTYPE html>
<html><body><table class="safe"><tr><td>Hello &amp; goodbye</td></tr></table></body></html>
EOF
cp "$t/clean.html" "$t/expected"
python3 filter.py "$t/clean.html" || exit 1
cmp -s "$t/clean.html" "$t/expected" || { echo 'clean HTML formatting changed'; exit 1; }
cat >"$t/bad.html" <<'EOF'
<html><body onload="alert(1)"><p>KEEP-ME</p><script>alert(2)</script><iframe src='javascript:alert(3)'>bad</iframe><a href="javascript:alert(4)" onclick='alert(5)'>safe link</a><img src=x onerror=alert(6)></body></html>
EOF
python3 filter.py "$t/bad.html" || exit 1
python3 - "$t/bad.html" <<'PY'
import re,sys
s=open(sys.argv[1]).read().lower()
assert 'keep-me' in s and 'safe link' in s
assert not re.search(r'<\s*(script|iframe|frame|object|embed)\b',s)
assert not re.search(r'\son[a-z0-9_-]+\s*=',s)
assert not re.search(r'(javascript|vbscript)\s*:',s)
PY
