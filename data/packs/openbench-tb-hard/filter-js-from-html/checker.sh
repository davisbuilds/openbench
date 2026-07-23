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
<html><body onload="alert(1)"><p>KEEP-ME</p><SCRIPT SRC=x>alert(2)</SCRIPT><iframe src='javascript:alert(3)'>bad</iframe><a href="java&#x73;cript:alert(4)" onclick='alert(5)'>safe link</a><img src=x onerror=alert(6)><svg onload=alert(7)></svg><a href=javascript:alert(71)>unquoted</a><form action=vbscript:msgbox(72)></form><form action='vbscript:msgbox(8)'><button formaction="data:text/html,<script>alert(9)</script>">go</button></form><div style="background:url(javascript:alert(10))">styled</div><object data=x>bad</object><meta http-equiv="refresh" content="0;url=java&#x73;cript:alert(11)"></body></html>
EOF
python3 filter.py "$t/bad.html" || exit 1
python3 - "$t/bad.html" <<'PY'
import html,re,sys
s=open(sys.argv[1]).read(); low=html.unescape(s).lower()
assert 'keep-me' in low and 'safe link' in low and '>go<' in low and 'styled' in low and 'unquoted' in low
assert not re.search(r'<\s*(script|iframe|frame|object|embed)\b',low)
assert not re.search(r'\s(?:on[a-z0-9_-]+|srcdoc)\s*=',low)
compact=re.sub(r'[\s\x00-\x20]+','',low)
assert 'javascript:' not in compact and 'vbscript:' not in compact and 'data:text/html' not in compact
assert 'expression(' not in compact
PY
