# Leaderboard site (`obench site`)

`obench site build` writes the browsable leaderboard front end for the GitHub
Pages site: **`docs/board.html`** plus the machine-readable **`docs/board.json`**
behind it. It covers both benchmark families in one page, with a tab each:

- **Harness Bench** — verified `results.jsonl` publish bundles, aggregated
  exactly as [`obench leaderboard`](leaderboard.md) does, then enriched with
  median wall time and `$/solve`.
- **Router Bench** — verified `router_bench` evidence bundles, aggregated by
  `obench/router_report.py`, including the Gateway Tax contrast table.
- **Releases** — the `releases.json` / `community.json` / `packs.json` manifests.
- **Methodology** — denominators, intervals, token bases, and the
  comparability rules, in one place next to the numbers.

`docs/leaderboard.html` remains as the no-JavaScript fallback and is linked from
the page's `<noscript>` block. Both are generated; neither is edited by hand.

## Build

```bash
obench leaderboard build   # refreshes leaderboard.html/.json and index.html
obench site build          # writes board.html + board.json
```

| Flag | Effect |
|------|--------|
| `--site-dir PATH` | Pages root (default: `docs/`) |
| `--community-dir PATH` | Extra harness scan root (default: `data/community` when present) |
| `--no-community-dir` | Only scan `site-dir/releases` + `site-dir/community` |
| `--router-dir PATH` | Router bundle root, repeatable (default: `<site-dir>/router`) |

The build is deterministic: the same inputs produce byte-identical output, so
regenerating produces a clean diff.

## Publishing a Router Bench board

The Router tab reads verified bundles from `docs/router/*/`. Each directory is
what `obench router publish` produces — `provenance.json` with
`bundle_kind = "router_bench"`, the sanitized `results.jsonl`, and the
`experiment` / `policy` / `catalog` / `prices` snapshots.

```bash
obench router run experiment.toml --results results/gw.jsonl
obench router publish results/gw.jsonl experiment.toml docs/router/2026-07-23-gateway-tax
obench site build
```

Every bundle is re-verified with `router_publish.verify_bundle()` at build time.
A directory that fails verification, is not a router bundle, or whose rows do
not aggregate into a Gateway Tax report is listed as skipped rather than
silently dropped. Optional titles and dates come from a `docs/router.json`
manifest keyed by directory name, in the same shape as `releases.json`:

```json
[
  {
    "id": "2026-07-23-gateway-tax",
    "title": "Gateway Tax: gpt-4o-mini, direct vs OpenRouter / Vercel",
    "date": "2026-07-23",
    "path": "router/2026-07-23-gateway-tax/index.html"
  }
]
```

## What the columns mean

**Harness Bench.** Solve rate on countable cells with a Wilson 95% interval
drawn as a bar; solved/n; median wall time among *solved* cells; total tokens
per solve from split fields (never the vendor aggregate); `$/solve` when the
model has a configured price in `prices.json`; and token-basis chips
(`proxy-measured`, `vendor_split`, `self-reported`) because those bases are not
interchangeable.

**Router Bench.** Per route: solve rate, mean checker score, availability, and
latency, each with a task-bootstrap 95% interval; plus `$/solve` on the
best-covered cost basis, preferring `invoice_reconciled`, then
`router_reported`, then `frozen_list_estimate`. A basis covering less than every
call is flagged with its coverage. The **Gateway tax** table shows the paired,
task-weighted difference of each gateway arm from its direct control arm — an
interval spanning zero is not a detected effect.

## Design decisions worth keeping

The page is an instrument readout, not a marketing page, so the craft went into
information design rather than ornament.

- **Type carries the split.** Prose is set in the system sans; every identifier
  the benchmark *names* and every value it *measures* — harness, model, arm id,
  rate, latency, digest — is set in mono, because those are readings, not
  writing. No webfont is loaded: a data-URI face would bloat a committed file
  and a CDN link would break the no-third-party-assets rule.
- **One shared interval scale.** Solve-rate intervals plot against the same
  0–100% track, gridded at 25% steps with the axis labelled once in the column
  header, so rows compare down the column instead of each bar being its own
  private scale.
- **Contrasts diverge about zero.** Gateway-tax deltas plot on a zero-centred
  axis with a shared per-column domain. An interval covering zero is drawn
  neutral grey, so "no detected effect" is something you see rather than
  something you read in a caption.
- **The diverging pair is validated, not eyeballed.** Better/worse use blue
  `#2a78d6`/`#3987e5` against orange `#eb6834`/`#d95926` — a warm/cool pair that
  clears CVD separation, the normal-vision floor, and 3:1 contrast in both
  modes. Green-vs-red was rejected: it fails deutan separation badly (ΔE 4.1).
  Direction is also carried by the sign and by which side of zero the bar sits
  on, so it never depends on colour alone.
- **Empty columns are dropped, not dashed.** A measure no arm reports (`$/solve`
  with no configured price) is removed entirely, and the model column only
  appears when a board compares more than one model.
- **Amber is reserved.** It marks disclosed caveats, excluded blocks, and
  partial cost coverage, and always ships with a label.

Both themes are token-level: `:root` holds light, the `prefers-color-scheme`
block carries the OS preference behind a `:where(:not([data-theme="light"]))`
guard, and explicit `[data-theme]` scopes let the in-page toggle win in both
directions — including flipping `color-scheme` back for native form controls.

## Constraints the page keeps

- **Self-contained.** The data is embedded in the page, so it works from
  `file://`. No CDN, no fonts, no network fetches, no build step.
- **Never blends bundles.** Each board is one bundle. There is no cross-bundle
  or cross-family aggregate score, and the two families share no denominators.
- **Caveats sit next to scores.** A bundle's disclosed caveats are shown on its
  board, not in a footnote, and boards with caveats can be filtered out.
- **Unverifiable results are not ranked.** HTML-only release cards and bundles
  failing digest verification appear in a "not ranked" list with the reason.

Regenerate after adding or updating any bundle so the Pages site stays in sync.
