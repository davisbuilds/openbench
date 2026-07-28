# Leaderboard site (`obench site`)

`obench site build` writes the GitHub Pages site: **`docs/index.html`** — the
landing page *is* the leaderboard — plus the machine-readable
**`docs/board.json`** alongside it. It keeps each benchmark family in a
separate tab:

- **Harness Bench** — verified `results.jsonl` publish bundles, aggregated
  exactly as [`obench leaderboard`](leaderboard.md) does, then enriched with
  median wall time and `$/solve`.
- **Gateway Bench** — verified request-level bundles with cold and warm request
  telemetry, an absolute route leaderboard, and paired route deltas.
- **Releases** — the `releases.json` / `community.json` / `packs.json` manifests.
- **Methodology** — denominators, intervals, token bases, and the
  comparability rules, in one place next to the numbers.

Every table is rendered in Python at build time; the page's script only
*enhances* what is already there — it re-orders rows, hides them, and switches
tabs. With JavaScript off the page is a single scrolling document containing
every board, and the tab bar degrades to jump links. There is one renderer, and
no separate fallback page.

The page is generated, never edited by hand.

An optional `site-meta.json` in the site root supplies the official
deployment's canonical URL and absolute social-preview image URL. Only
HTTP(S) values render. The preview remains a committed site asset, so it can
show the published table rather than decorative sample data.

## Build

```bash
obench site build          # writes docs/index.html + docs/board.json
```

`obench leaderboard build` is kept as an alias so existing scripts keep working;
it builds exactly the same artifacts. Publishing a release
(`obench publish --site-dir`), accepting a community bundle, and updating the
pack index all rebuild the landing page automatically.

| Flag | Effect |
|------|--------|
| `--site-dir PATH` | Pages root (default: `docs/`) |
| `--community-dir PATH` | Extra harness scan root (default: `data/community` when present) |
| `--no-community-dir` | Only scan `site-dir/releases` + `site-dir/community` |
| `--gateway-probe-dir PATH` | Public Gateway Bench bundle root, repeatable (default: `<site-dir>/gateway-probe`) |

The build is deterministic: the same inputs produce byte-identical output, so
regenerating produces a clean diff.

## Publishing a Gateway Bench board

The Gateway Bench tab reads public request-level bundles from
`docs/gateway-probe/*/`. Each directory must be the exact output of
`obench gateway probe publish`; the site re-verifies it with
`gateway_probe_publish.verify_bundle()` before reading its schema-v4 report.
Digest drift, extra files, symlinks, schema drift, or a report that does not
exactly recompute is listed as skipped and never rendered. The internal
`gateway_probe` identifiers remain part of the bundle and command contracts;
they are not a separate public benchmark family.

```bash
obench gateway probe publish RUN_DIR docs/gateway-probe/2026-07-27-managed
obench gateway probe verify docs/gateway-probe/2026-07-27-managed
obench site build
```

Optional title, date, and page metadata come from `docs/gateway-probe.json`,
keyed by bundle directory:

```json
[
  {
    "id": "2026-07-27-managed",
    "title": "Gateway Bench: managed routes",
    "date": "2026-07-27",
    "output_token_limit": 128
  }
]
```

When `output_token_limit` is a positive integer, the generated bundle panel
counts verified public rows whose measured `output_tokens` equal that configured
request limit, split by route and cold/warm condition. Equality is disclosed as
a cap proxy rather than proof of truncation because public bundles do not retain
finish reasons. Missing or invalid metadata omits this disclosure.

The verified bundle directory has an exact file set and contains no
`index.html`. Omit `path` unless a separate release page exists outside that
directory.

Request attempts and complete cold/warm blocks are never merged with Harness
Bench coding-agent cells or their claims. Legacy coding-agent gateway workload
bundles under `docs/gateway/` are not included in the generated site.

The route leaderboard ranks managed gateways by an absolute, cost-free
**OpenBench Composite** on a 0–100 scale. Before the availability multiplier,
the normalized weights are:

| Component | Weight |
|-----------|-------:|
| Cold TTFT p50 | 30% |
| Cold TTFT p95 | 15% |
| Warm TTFT p50 | 30% |
| Warm TTFT p95 | 15% |
| Output throughput | 10% |

For a latency value `t` in seconds, its subscore is
`clamp(100 × (1 - t / 20), 0, 100)`. For output throughput `r` in tokens per
second, its subscore is `clamp((r - 5) / (200 - 5) × 100, 0, 100)`. The
throughput input is the warm p50 reading; cold TTFT includes cold connection
setup. The weighted score is multiplied by the combined cold-and-warm request
success rate **linearly**.

Direct OpenAI does not appear in the ranking. It remains the reference for the
paired request deltas. The detailed cold, warm, connection-setup, and paired
delta tables are the factual record, including bootstrap 95% intervals and
paired-block coverage. Frozen-list prices stay sealed in the bundle for
reproducibility but are not a composite component and are not displayed because
market prices drift.

## What the columns mean

**Harness Bench.** Solve rate on matched task/trial rows with a Wilson 95%
interval drawn as a bar; solved/n; median wall time among *solved* cells; fresh,
uncached-input, output, cache-read, and cache-write traffic per solve; `$/solve`
when the model has a configured price in `prices.json`; and exact telemetry
source, basis, and row coverage.

One complete telemetry lane is selected for each arm: complete counting-proxy
splits are preferred, otherwise complete native harness splits are used. Sources
are never mixed row by row. Incomplete lanes fail closed and display coverage
instead of a potentially biased token figure. Per-solve token traffic sums every
matched attempt, including failures, and divides by solved cells.

**Gateway Bench.** Cold and warm request conditions render in separate tables.
Each route row includes the provider logo, followed by TTFT, stream total,
response headers, first body byte, throughput p50/p95, and total, cached-input,
and cache-write tokens with coverage. The raw label is **TTFT**. Cold TTFT
includes DNS, TCP, and TLS setup, matching the composite input; warm TTFT starts
when the measured request is sent.
Response headers are not labeled TTFB. Measured and
charged cost, including the warm primer request, remain in the sealed result
artifact for spend auditing but are not displayed or included in the composite.

The board displays exact complete/scheduled block counts for both conditions
prominently, without a qualitative sample-size label. A compact cold-only table
shows provider logos plus DNS, TCP, and TLS setup p50/p95. Paired request
contrasts are limited to gateway-minus-direct response-headers and TTFT medians
with bootstrap 95% intervals and pair coverage. Every delta is gateway minus
Direct OpenAI. Because both displayed deltas are latency metrics, positive
values mean the gateway is slower/worse and negative values mean it is
faster/better. Because Direct OpenAI is fixed by the experiment, the paired
table does not repeat a `vs direct` column.

## Contact

The generated Contact section links directly to
[GitHub issue creation](https://github.com/minghinmatthewlam/openbench/issues/new)
and [@mattlam_ on X](https://x.com/mattlam_).

## Design decisions worth keeping

The page is treated as a **published measurement**, not a dashboard — it is
meant to be read, shared, and cited, so it gets an editorial treatment rather
than an admin-panel one.

- **Three type roles.** A serif display face carries the headline and board
  titles; a sans carries prose and small labels; a mono carries every
  identifier the benchmark names and every value it measures, because those
  are readings rather than writing. No webfont is loaded — a CDN link would
  break the no-third-party-assets rule and a data-URI face would commit a
  licensed asset to the repo. If that trade is ever worth making, embedding a
  subsetted SIL OFL face is the intended route.
- **Ink on paper; colour is reserved for data.** Chrome is ink and hairlines
  only. Colour appears exclusively on the interval marks and the diverging
  contrast poles, so a coloured pixel always means something measured.
- **Rules, not boxes.** Boards are records separated by hairlines rather than
  cards with borders and shadows, so the data sits on the page.
- **The lede reports coverage, not a conclusion.** The headline names the two
  result families and the dateline counts included harnesses, models,
  result-sealed bundles, valid result rows, and matched result rows. A result
  seal verifies the bundled JSONL hash; current-tree task digest status is a
  separate check because task definitions can change after an archived run.
  Results stay in the tables for readers to interpret.
- **Tables sort values without assigning ordinal ranks.** Equal estimates are
  not presented as first, second, and third, and no row receives visual
  winner treatment.
- **One shared interval scale.** Solve-rate intervals plot against the same
  0–100% track, gridded at 25% steps with the axis labelled once in the column
  header, so rows compare down the column instead of each bar being its own
  private scale.
- **Contrasts diverge about zero.** Gateway-tax deltas plot on a zero-centred
  axis with a shared per-column domain. An interval covering zero is drawn
  neutral, so "no detected effect" is something you see rather than something
  you read in a caption.
- **The diverging pair is validated, not eyeballed.** Better/worse use blue
  `#2a78d6`/`#3987e5` against orange `#eb6834`/`#d95926` — a warm/cool pair that
  clears CVD separation, the normal-vision floor, and 3:1 contrast in both
  modes. Green-vs-red was rejected: it fails deutan separation badly (ΔE 4.1).
  Direction is also carried by the sign and by which side of zero the bar sits
  on, so it never depends on colour alone.
- **Plot width is a per-table budget.** A table with four contrast columns
  cannot give each the width a lone solve-rate column gets, so `_render_table`
  sizes `--plot-w` from the number of plot columns and drops the printed
  interval on dense tables, where the bar already carries it.
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

- **Self-contained.** Content is rendered into the page, so it works from
  `file://`. No CDN, no fonts, no network fetches, no build step.
- **Never blends bundles.** Each board is one bundle. There is no cross-bundle
  or cross-family aggregate score, and the two families share no denominators.
- **Caveats sit next to scores.** A bundle's disclosed caveats are shown on its
  board, not in a footnote, and boards with caveats can be filtered out.
- **Unverifiable results are not ranked.** HTML-only release cards and bundles
  failing digest verification appear in a "not ranked" list with the reason.

Regenerate after adding or updating any bundle so the Pages site stays in sync.
