#!/usr/bin/env bash
set -uo pipefail
python3 - "${TASK_DIR}/checker_data/valid-arms.json" <<'PY'
import json,pathlib,re,sys
vector1 = "tctagaaataattttgtttaactttaagaaggagatatacatatg"
vector2 = "agcaagggcgaggagctgttcaccggggtggtgcccatcctggtc"
insert = "agtagattagaagaagaattaagaagaagattaacagaa"
def rc(s): return s.translate(str.maketrans('atcg','tagc'))[::-1]
p=pathlib.Path('primers.fasta')
if not p.is_file(): raise SystemExit('missing primers.fasta')
lines=p.read_text().splitlines()
if len(lines)!=4: raise SystemExit('invalid FASTA layout')
fwd,rev=lines[1].lower(),lines[3].lower()
if not re.fullmatch('[atcg]+',fwd+rev): raise SystemExit('invalid primer alphabet')
joined=rc(rev)+fwd
start=joined.find(insert)
if start<0: raise SystemExit('primers do not encode insert')
annealed_rev,annealed_fwd=joined[:start],joined[start+len(insert):]
if vector1[-len(annealed_rev):] != annealed_rev or vector2[:len(annealed_fwd)] != annealed_fwd: raise SystemExit('primer/vector overlap mismatch')
# Complete checker-owned matrix generated with the upstream pinned oligotm flags
# for all permitted 15..45-base vector suffix/prefix combinations.
valid={(row[0],row[1]) for row in json.load(open(sys.argv[1]))}
if (annealed_rev,annealed_fwd) not in valid: raise SystemExit('oligotm range/pair constraint mismatch')
PY
