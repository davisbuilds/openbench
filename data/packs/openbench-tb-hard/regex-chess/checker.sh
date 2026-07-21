#!/usr/bin/env bash
set -uo pipefail
python3 - "${TASK_DIR}/checker_data/oracle-fens.json" <<'PY'
import json,re,sys,os
if not os.path.isfile('re.json'): raise SystemExit('missing re.json')
if os.path.getsize('re.json')>=10_000_000: raise SystemExit('re.json too large')
pairs=json.load(open('re.json'))
if not isinstance(pairs,list) or len(pairs)>=100_000: raise SystemExit('invalid regex list')
for p in pairs:
 if not isinstance(p,list) or len(p)!=2 or not all(isinstance(x,str) for x in p): raise SystemExit('invalid pair')
fixtures=json.load(open(sys.argv[1]))
for fen,expected in fixtures:
 out=fen
 for pattern,repl in pairs: out=re.sub(pattern,repl,out)
 got=[' '.join(x.split(' ')[:-2]) for x in out.split('\n')]
 def norm(x):
  a=x.split(); a[-1]='-'; return ' '.join(a)
 if sorted(map(norm,got))!=sorted(map(norm,expected)): raise SystemExit('legal-move oracle mismatch')
PY
