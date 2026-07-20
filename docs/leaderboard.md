# Static leaderboard (verified bundles)

The GitHub Pages site includes a quick-view **leaderboard** built from
machine-readable publish bundles — directories that ship `results.jsonl` plus
`provenance.json` under `docs/releases/*/` and `docs/community/*/` (and,
when present, `data/community/*/`).

## Comparability is the product

The page **does not** produce a global cross-bundle harness ranking.

- Each board is **one bundle** (one self-consistent comparison).
- Arms are always `(harness, model)`, never harness alone.
- Within a bundle, when two or more arms exist, denominators use the
  matched `(task, trial)` table — the same philosophy as `obench compare`.
- HTML-only release cards (no `results.jsonl`) are listed as skipped; they
  cannot be ranked from digests alone.
- Identical `results_sha256` values (e.g. a community seed of an official
  release) are shown once, with aliases under “also seen as”.

## Build

```bash
obench leaderboard build
# writes docs/leaderboard.html + docs/leaderboard.json
# and refreshes docs/index.html (leaderboard link via _site_index)
```

Useful flags:

| Flag | Effect |
|------|--------|
| `--site-dir PATH` | Pages root (default: `docs/`) |
| `--community-dir PATH` | Extra scan root (default: `data/community` when present) |
| `--no-community-dir` | Only scan `site-dir/releases` + `site-dir/community` |
| `--no-refresh-index` | Do not regenerate `index.html` |

## Caveats

Bundles can declare caveats in `leaderboard.toml`:

```toml
caveats = [
  "Short, human-readable caveat shown on the leaderboard.",
]
```

If `index.html` contains a section with `id="caveats"`, those `<li>` texts
are picked up automatically when no TOML list is present (or merged with it).

## Metrics

Per arm inside a bundle:

- Solve rate on countable cells (infra / rate-limit excluded), with Wilson 95% CI
- Effective tokens / solve (self-reported `tokens` when present, else
  proxy-measured fresh total) plus token-basis tags
- Cell counts and links back to the bundle card / `results.jsonl`

Regenerate after adding or updating a verified bundle so the Pages site stays
in sync.
