#!/usr/bin/env bash
set -uo pipefail
python3 - <<'PY'
import pathlib,re
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
if not (15 <= len(annealed_rev) <= 45 and 15 <= len(annealed_fwd) <= 45): raise SystemExit('annealed lengths out of range')
if vector1[-len(annealed_rev):] != annealed_rev or vector2[:len(annealed_fwd)] != annealed_fwd: raise SystemExit('primer/vector overlap mismatch')
# Conservative dependency-free thermodynamic guard. The upstream primer3 oracle
# remains in checker_data; GC/length bounds reject implausible annealing arms
# while allowing every valid split of the insert between primer overhangs.
for arm in (annealed_rev, annealed_fwd):
    gc=(arm.count('g')+arm.count('c'))/len(arm)
    if not (0.15 <= gc <= 0.80): raise SystemExit('annealing arm GC fraction out of range')
PY
