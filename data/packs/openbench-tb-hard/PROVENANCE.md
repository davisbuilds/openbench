# openbench/tb-hard provenance

## Upstream task snapshot

- Project: [Terminal-Bench 2](https://github.com/laude-institute/terminal-bench-2)
- Task commit: `2fd12b88aafdd04a52c298e3940bcb189f9766d6` (2026-04-29)
- Upstream license: Apache-2.0 (`LICENSE` at the pinned commit)
- Import path: `obench import harbor`; verifier assets remain checker-owned under `checker_data/`.

## Published frontier-rate source and method

Rates are derived from the public, verified [Terminal-Bench 2.0 leaderboard submissions dataset](https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard), pinned at `572b2614be2c0cb2527e14f5b1e4026f1072e6c1` (2026-05-15). The frontier panel is:

- `OpenCode__Claude-Opus-4.5`
- `Mux__Claude-Opus-4.5`
- `Mux__GPT-5.2`
- `Ante__Gemini-3-Pro-Preview`
- `Deep-Agents__GPT-5.2-Codex`

For every available published trial, rate is `sum(verifier/reward.txt) / trial count`. Counts vary when a submission omitted a task or published fewer trials. These are reproducible published-artifact rates, not new OpenBench runs. Nine tasks are at or below 25%; `polyglot-rust-c` is the nearest domain-diversifying boundary task at 38.5%, within the requested approximate 0–35% band.

| Task | Domain | Solved / trials | Frontier solve rate |
|---|---|---:|---:|
| `filter-js-from-html` | web security | 0 / 12 | 0.0% |
| `regex-chess` | formal languages / chess | 0 / 10 | 0.0% |
| `sam-cell-seg` | vision / biology | 0 / 13 | 0.0% |
| `torch-pipeline-parallelism` | distributed ML | 0 / 13 | 0.0% |
| `torch-tensor-parallelism` | distributed ML | 0 / 13 | 0.0% |
| `dna-insert` | bioinformatics | 1 / 13 | 7.7% |
| `video-processing` | video analytics | 1 / 13 | 7.7% |
| `extract-moves-from-video` | video / chess | 2 / 13 | 15.4% |
| `model-extraction-relu-logits` | adversarial ML | 3 / 12 | 25.0% |
| `polyglot-rust-c` | systems / polyglot binaries | 5 / 13 | 38.5% |

## Selection and oracle controls

Names were checked against both existing imported collections under `tasks-imported/`; there is no overlap. Selection favors domain diversity rather than filling the pack with tied ML or cryptography tasks.

Importer-materialized solutions were retained where clean. Three newer-format tasks needed deterministic manual materialization or checker portability hardening:

- `regex-chess`: decoded the upstream `solve.sh` payload; the checker uses four pinned legal-move oracle positions to fit OpenBench's 120-second polarity limit.
- `dna-insert`: materialized the upstream primer output; checker owns the exact validated primer oracle without requiring system `primer3` during validation.
- `filter-js-from-html`: replaced the formatting-altering upstream reference with a stdlib, formatting-preserving solution and checker-owned clean/malicious fixtures.
- `video-processing`: corrected the imported absolute materialization path to write `output.toml` in the current workspace and emit the instructed schema.

Every checker fails untouched and passes after its `solution/` overlay; `obench validate` is the admission proof.
