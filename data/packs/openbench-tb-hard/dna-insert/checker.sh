#!/usr/bin/env bash
set -uo pipefail
python3 - <<'PY'
import pathlib,re,sys
p=pathlib.Path('primers.fasta')
if not p.is_file(): raise SystemExit('missing primers.fasta')
lines=p.read_text().splitlines()
if len(lines)!=4 or lines[0]!='forward_primer' or lines[2]!='reverse_primer': raise SystemExit('invalid FASTA layout')
fwd,rev=lines[1].lower(),lines[3].lower()
if not re.fullmatch('[atcg]+',fwd+rev): raise SystemExit('invalid primer alphabet')
# Pinned upstream oracle output; the hidden oracle remains checker-owned.
expected=('taagaagaagattaacagaaagcaagggcgaggag','attcttcttctaatctactcatatgtatatctccttcttaaagttaaacaaaattatttc')
if (fwd,rev)!=expected: raise SystemExit('primers do not satisfy insert/overlap oracle')
PY
