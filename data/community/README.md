# Community publish-bundle submissions

Third parties land a verified `obench publish` bundle here so CI can re-run
`obench verify` and maintainers can sync accepted cards onto the public
GitHub Pages site (`docs/community.json` + `docs/community/<id>/`).

## Layout

```
data/community/
  README.md                          # this file
  <submitter>-<slug>/                # one directory per submission
    submission.toml                  # submitter metadata (required)
    index.html                       # publish-bundle comparison card
    results.jsonl                    # filtered claim rows (no transcripts)
    provenance.json                  # digests + digest_scheme
    README.md                        # re-verify instructions from publish
```

Directory names must be path-safe: `[A-Za-z0-9][A-Za-z0-9._-]*`
(convention: GitHub handle + short slug, e.g. `alice-my-cli-vs-pi`).

## `submission.toml`

| Key | Required | Meaning |
|-----|----------|---------|
| `submitter` | yes | GitHub-style handle |
| `date` | yes | Claim date (`YYYY-MM-DD`) |
| `claim` | yes | One-line summary of what is being claimed |
| `title` | no | Card title (defaults to `claim`) |
| `link` | no | Optional http(s) URL (PR, blog post, repo) |

Example:

```toml
submitter = "alice"
date = "2026-07-20"
title = "my-cli vs pi on deepseek-v4-flash"
claim = "my-cli beats pi on fix-failing-test + build-a-cli (2 trials)."
link = "https://github.com/alice/my-cli"
```

## Workflow

1. Run the matrix, then `obench publish` and `obench verify` locally.
2. Run the candidate gate and archive a PASS record (see `docs/publish.md`).
3. Open a PR that adds `data/community/<submitter>-<slug>/` with the bundle
   files plus `submission.toml`.
4. CI runs `obench community verify` on every bundle under this tree and fails
   on any FAIL verdict.
5. After merge, maintainers sync to the site with
   `obench community sync` (writes `docs/community.json`, copies cards under
   `docs/community/`, regenerates `docs/index.html`).

See **Submit your results** in [`docs/publish.md`](../../docs/publish.md).
