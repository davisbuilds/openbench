# openbench/tb-hard

Ten frontier-discriminative Terminal-Bench 2.0 tasks spanning web security, formal languages, systems, video, biology, and distributed/adversarial ML. Published frontier-panel solve rates are 0–38.5% (nine are 0–25%); see [PROVENANCE.md](PROVENANCE.md).

```bash
python3 -m obench.cli pack install openbench/tb-hard@1.0.0 \
  --from data/packs/openbench-tb-hard
python3 -m obench.cli pack verify openbench/tb-hard@1.0.0
python3 -m obench.cli validate \
  --tasks-dir .openbench/packs/openbench/tb-hard/1.0.0
```

Some tasks retain `REQUIREMENTS.md` because their upstream Harbor image installs system or ML dependencies. OpenBench polarity validation covers every task using its materialized `solution/` overlay.
