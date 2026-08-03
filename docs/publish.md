# Publishing a shareable comparison claim

Third-party harness authors who have run their candidate against stock
OpenBench arms can turn those results into a **self-contained, postable
artifact** with one command. That show-off loop is how the framework grows:
prove a claim, share the bundle, let others re-verify digests locally.

## End-to-end workflow

```bash
# 1. Admit the candidate (schema dry-run first; --live spends tokens).
obench gate path/to/my-cli.toml --model deepseek-v4-flash
# Live checks (when you are ready to spend):
mkdir -p data
obench gate path/to/my-cli.toml \
  --model deepseek-v4-flash --live > data/my-cli-gate.json

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
| `provenance.json` | `obench` version, `digest_scheme`, per-arm identity digests (`candidate_provenance`), per-task content digests, models, trial counts, SHA-256 of `results.jsonl`, and a per-run `harbor_import_evidence` manifest when Harbor-imported rows are present |
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
- **Completeness is fail-closed.** Publish refuses when arms disagree on task
  sets, models, or trial cells, or when a candidate has no exact live PASS gate
  record under `data/`, `.openbench/gate/`, or `results/gate/`. Use
  `--allow-incomplete` only for an intentionally caveated artifact; the
  resulting warnings remain in terminal output, provenance, and HTML.
- **Harbor imports have a strict publication schema.** A row marked as Harbor
  execution must contain the complete normalized importer provenance and agree
  with its workspace and usage fields. Publish retains the Harbor build, job
  and trial identifiers, lock/result/reward/ATIF/verifier/artifact/workspace
  digests, task hashes, mapping semantics, and Harbor usage source. It drops
  every non-allowlisted row field, including raw trajectory, session,
  transcript, and workspace paths or credential material. Missing, partial,
  contradictory, or extended Harbor provenance refuses publication before the
  output directory is created; `--allow-incomplete` and
  `--allow-pii-override` do not bypass this validation.

## What `obench verify` proves

```bash
obench verify openbench-publish/my-claim
# PASS/FAIL per check, then VERDICT: PASS|FAIL
```

- The bundled `results.jsonl` still matches `provenance.json`'s `results_sha256`.
- For Harbor-imported rows, the per-run `harbor_import_evidence` manifest
  exactly matches the safe normalized evidence in `results.jsonl`. This binds
  the ATIF, verifier, artifact, final-workspace, lock/result/reward, task, and
  usage-provenance claims without publishing the underlying private artifacts.
- For each Harbor task, the structured executed digest is scheme 2 and equals
  the task digest recorded by publication. `obench publish` also requires it
  to match the local task tree before writing the bundle.
- Using the evidence-bound exporter parameters, publish and verify reproduce
  the Harbor 0.20.0 package content hash from a canonical re-export and require
  it to equal the locked Harbor task digest. A modified post-export task cannot
  retain the original OpenBench task claim.
- When local task trees are available (`--tasks-dir` or CWD discovery), each
  recorded task content digest still matches under the bundle's
  `digest_scheme`:
  - **scheme 2** (current publish default):
    `hash(instruction.md + checker.sh + workspace|workspace.toml + checker_data/)`
  - **scheme 1** (legacy; also assumed when `digest_scheme` is absent):
    same inputs **without** `checker_data/`, so pre-scheme-2 bundles still
    verify honestly against untampered task trees
  A missing `content_digest` is a FAIL (not skipped). Verify prints
  `scheme=N` in each task-digest check detail.

## What verify does NOT prove

- Runs were not **cherry-picked** or **rerun until green**.
- Live candidate-gate checks succeeded on the publisher's machine.
- Auth material or LOCAL-ONLY transcripts were reviewed (transcripts are never
  in the bundle).

**Recommendation:** publish the **full matrix** (every planned harness × task ×
trial cell), not a curated subset of green cells.

## Submit your results

Want the claim listed on the public OpenBench Pages site under **Community
results**? After a local PASS verify:

1. **Publish** a bundle (`obench publish …`) and confirm
   `obench verify <bundle>` prints `VERDICT: PASS`.
2. **Gate** the candidate and archive a PASS record (see the end-to-end
   workflow above) so reviewers can see admission evidence.
3. **Open a PR** that adds the bundle under
   `data/community/<submitter>-<slug>/` with the four publish files
   (`index.html`, `results.jsonl`, `provenance.json`, `README.md`) plus a
   small `submission.toml` (submitter GitHub handle, date, claim summary,
   optional link). Layout details:
   [`data/community/README.md`](../data/community/README.md).
4. **CI re-verifies** every bundle under `data/community/` via
   `obench community verify` (GitHub Actions workflow `community.yml`).
   Any FAIL verdict blocks the PR.
5. After merge, maintainers sync accepted cards onto the site with
   `obench community sync` (writes `docs/community.json`, copies cards to
   `docs/community/<id>/`, regenerates `docs/index.html`).

Listing proves digests still match — not that runs were not cherry-picked.
Publish the full matrix whenever possible.

## Related

- Bring-your-own harness manifests and the admission gate:
  [`docs/byo-harnesses.md`](byo-harnesses.md)
- Static release-site HTML (separate from shareable claim bundles):
  [`docs/REPORT_PAGE.md`](REPORT_PAGE.md), [`docs/README.md`](README.md)
- Per-bundle leaderboard from verified `results.jsonl` bundles:
  [`docs/leaderboard.md`](leaderboard.md)
- Browsable two-family leaderboard site (harness + gateway):
  [`docs/site.md`](site.md)
- Community submission tree:
  [`data/community/README.md`](../data/community/README.md)
