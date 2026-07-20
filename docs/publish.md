# Publishing a shareable comparison claim

Third-party harness authors who have run their candidate against stock
OpenBench arms can turn those results into a **self-contained, postable
artifact** with one command. That show-off loop is how the framework grows:
prove a claim, share the bundle, let others re-verify digests locally.

## End-to-end workflow

```bash
# 1. Admit the candidate (schema dry-run first; --live spends tokens).
obench gate path/to/my-cli.toml --model deepseek-v4-flash
# Archive the printed JSON somewhere durable, e.g.:
#   mkdir -p data && obench gate ... > data/my-cli-gate.json
# Live checks (when you are ready to spend):
obench gate path/to/my-cli.toml --model deepseek-v4-flash --live

# 2. Run a matrix: candidate + stock arms, same tasks / model / trials.
obench run --harness null,pi \
  --candidate path/to/my-cli.toml \
  --task fix-failing-test,build-a-cli \
  --model deepseek-v4-flash \
  --trials 2 \
  --results-path results/my-claim.jsonl

# 3. Publish a shareable bundle (no transcripts ever leave the machine).
obench publish \
  --results-path results/my-claim.jsonl \
  --candidate my-cli \
  --out openbench-publish/my-claim

# 4. Re-verify locally, then post the directory (or zip it).
obench verify openbench-publish/my-claim
```

Post the bundle (HTML card + `results.jsonl` + `provenance.json` + README).
Readers open `index.html` and, if they have the same tasks checked out, run
`obench verify` themselves.

## What the bundle contains

| Path | Role |
|------|------|
| `index.html` | Self-contained HTML comparison card (candidate row(s) highlighted; Wilson CIs; mean score / wall; tokens/solve; token-basis badges: `unmetered` / `self-reported` / `proxy-measured`) |
| `results.jsonl` | Filtered rows for the claim — transcript fields stripped |
| `provenance.json` | `obench` version, per-arm identity digests (`candidate_provenance`), per-task content digests, models, trial counts, SHA-256 of `results.jsonl` |
| `README.md` | How to re-verify and what verify does / does not prove |

Default output directory: `./openbench-publish/<timestamp>/` (override with
`--out` or `--name`).

## Safety

- **Transcripts are LOCAL-ONLY.** Publish refuses if any result row references a
  transcript path or embeds a transcript body, and never copies a `transcripts/`
  tree into the bundle.
- **PII sanity check.** After writing the bundle, publish runs the same detectors
  as `obench.scrub --check` on high-signal classes (email, API keys, home paths,
  username, hostname). Digests intentionally look like hex blobs and are not
  treated as PII here. Hits refuse the publish; `--allow-pii-override` continues
  with a loud warning and is documented as dangerous.
- **Comparability warnings** (terminal + HTML) when arms disagree on task sets,
  models, or trial counts, and when a candidate has no archived gate PASS
  record under `data/`, `.openbench/gate/`, or `results/gate/`.

## What `obench verify` proves

```bash
obench verify openbench-publish/my-claim
# PASS/FAIL per check, then VERDICT: PASS|FAIL
```

- The bundled `results.jsonl` still matches `provenance.json`'s `results_sha256`.
- When local task trees are available (`--tasks-dir` or CWD discovery), each
  recorded task content digest still matches
  `hash(instruction.md + checker.sh + workspace|workspace.toml + checker_data/)`.
  A missing `content_digest` is a FAIL (not skipped).

## What verify does NOT prove

- Runs were not **cherry-picked** or **rerun until green**.
- Live candidate-gate checks succeeded on the publisher's machine.
- Auth material or LOCAL-ONLY transcripts were reviewed (transcripts are never
  in the bundle).

**Recommendation:** publish the **full matrix** (every planned harness × task ×
trial cell), not a curated subset of green cells.

## Related

- Bring-your-own harness manifests and the admission gate:
  [`docs/byo-harnesses.md`](byo-harnesses.md)
- Static release-site HTML (separate from shareable claim bundles):
  [`docs/REPORT_PAGE.md`](REPORT_PAGE.md), [`docs/README.md`](README.md)
