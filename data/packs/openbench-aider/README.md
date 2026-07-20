# openbench/aider

Seeded OpenBench **harness** pack wrapping `experiments/candidates/aider.toml`.

```bash
obench pack install openbench/aider@1.0.0 --from data/packs/openbench-aider
obench doctor --candidate openbench/aider@1.0.0 --model deepseek-v4-flash
obench gate openbench/aider@1.0.0 --model deepseek-v4-flash
```
