# Showcase: aider vs stock harnesses (pi, opencode)

First public show-off claim: **aider vs stock `pi` / `opencode` on the same open
model (`deepseek-v4-flash`), independently metered**, on three hard core tasks.

This runbook is for the Mac mini operator (`ssh mini`, checkout at
`~/dev/openbench`). Commands assume the **checkout root** as cwd unless noted.
It spends provider tokens only where marked **PAID**.

## Claim shape

| Axis | Value |
|------|--------|
| Candidate | `experiments/candidates/aider.toml` (`name = "aider"`) |
| Stock arms | `pi`, `opencode` |
| Model | `deepseek-v4-flash` (DeepSeek first-party; counting proxy) |
| Tasks | `make-ci-green`, `add-feature`, `misleading-error` |
| Trials | 3 per (arm × task) → **27 cells** |
| Negative control (optional) | `--harness null` (0 tokens; checkers must stay red) |

## Estimated cost

Committed M4 open-model matrix (`data/m4-2026-07-03/`) cost **~$1.02** total
across four models × two harnesses × these three tasks × 3 trials. The
DeepSeek stock slice alone was ~$0.04; adding aider as a third arm should stay
**well under $1**. Budget **~$1** as a safe operator ceiling (live gate smoke +
matrix + a small retry margin).

## Prerequisites (mini)

```bash
ssh mini
cd ~/dev/openbench
git pull

# Docker up (doctor checks daemon); harness CLIs installed.
command -v docker && docker info >/dev/null
command -v pi && command -v opencode && command -v aider

# Prefer an isolated aider install (do not pollute the global site-packages):
#   uv tool install aider-chat
# or:
#   python3 -m venv .venv-aider && .venv-aider/bin/pip install aider-chat
#   export PATH="$PWD/.venv-aider/bin:$PATH"

# Provider key — name only; value from your secret store / keys.env.
# Doctor also accepts ~/.openbench/keys.env when the var is not exported.
test -n "${DEEPSEEK_API_KEY:-}" || test -f ~/.openbench/keys.env

# Editable install of this checkout (if needed):
#   python3 -m venv .venv && .venv/bin/pip install -e .
#   export PATH="$PWD/.venv/bin:$PATH"
```

Preflight (no tokens):

```bash
obench doctor --candidate experiments/candidates/aider.toml \
  --model deepseek-v4-flash
obench doctor --harness pi,opencode --model deepseek-v4-flash
```

## 1. Admit the candidate (gate)

Dry schema/policy (safe; no harness launch):

```bash
obench gate experiments/candidates/aider.toml --model deepseek-v4-flash
```

**PAID** live admission (smoke + metering + failure honesty; spends tokens):

```bash
mkdir -p results/gate
obench gate experiments/candidates/aider.toml \
  --model deepseek-v4-flash --live \
  | tee results/gate/aider-deepseek-v4-flash.json
```

`obench publish` looks for a candidate-gate **PASS** archive under, in order:

1. `data/`
2. `.openbench/gate/`
3. `results/gate/`

Archive under **`results/gate/`** as above so the JSON is local (gitignored with
`results/`) and discoverable. If you later want the PASS record committed for
the release, copy the same file into `data/` (still found by publish).

The archived text must contain `"candidate": "aider"` (or `"candidate":"aider"`)
and `PASS`. Prefer the live JSON from `--live`, not a dry-run preview.

## 2. Run the matrix (**PAID**)

```bash
cd ~/dev/openbench
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ROOT="$PWD/results/showcase-aider-$STAMP"
mkdir -p "$ROOT"

obench run \
  --candidate experiments/candidates/aider.toml \
  --harness pi,opencode \
  --model deepseek-v4-flash \
  --task make-ci-green,add-feature,misleading-error \
  --trials 3 \
  --timeout 900 \
  --proxy \
  --results-path "$ROOT/results.jsonl"
```

Optional null control (no tokens; append or separate file):

```bash
obench run --harness null --model deepseek-v4-flash \
  --task make-ci-green,add-feature,misleading-error --trials 3 \
  --results-path "$ROOT/null-control.jsonl"
```

### Resume

Cells are skipped when their `run_id` already appears in the results JSONL.
If the process dies mid-matrix, re-run the **same** `obench run ... --results-path`
command; completed cells are not re-spent. Use `--force` only when you intend to
overwrite.

## 3. Publish + verify

```bash
# From checkout root so publish finds results/gate/ (and data/ / .openbench/gate/).
obench publish \
  --results-path "$ROOT/results.jsonl" \
  --candidate aider \
  --out "$ROOT/bundle" \
  --title "aider vs pi/opencode · deepseek-v4-flash"

obench verify "$ROOT/bundle"
# Expect: VERDICT: PASS
```

Share `$ROOT/bundle/` (HTML card + filtered `results.jsonl` + `provenance.json`).
Transcripts never leave the machine.

## Scrub reminder

Per-cell transcripts under the results sibling `transcripts/` tree are
**LOCAL-ONLY** and may contain absolute paths, usernames, and secrets.
`obench publish` strips transcript fields from the bundle, but do **not** zip or
post the raw `transcripts/` tree. Before any manual share of logs:

```bash
python3 -m obench.scrub transcripts/ --check
# If you must share scrubbed copies:
# python3 -m obench.scrub transcripts/ --out scrubbed/
# python3 -m obench.scrub scrubbed/ --check
```

## Token-free plumbing rehearsal (optional)

Same task × trial × model shape with the stock `null` harness (no API calls).
Useful to prove run → publish → verify before spending:

```bash
ROOT="$PWD/results/showcase-aider-rehearsal"
mkdir -p "$ROOT" results/gate

# Dry gate JSON archived where publish looks (mode=dry-run is fine for rehearsal).
obench gate experiments/candidates/aider.toml --model deepseek-v4-flash \
  > results/gate/aider-deepseek-v4-flash.dry.json

obench run --harness null --model deepseek-v4-flash \
  --task make-ci-green,add-feature,misleading-error --trials 3 \
  --results-path "$ROOT/results.jsonl"

obench publish --results-path "$ROOT/results.jsonl" \
  --out "$ROOT/bundle" --title "showcase plumbing rehearsal"
obench verify "$ROOT/bundle"
```

## Related

- Candidate contract / admission gate: [`docs/byo-harnesses.md`](../../docs/byo-harnesses.md)
- Publish / verify: [`docs/publish.md`](../../docs/publish.md)
- Cost precedent: [`data/m4-2026-07-03/README.md`](../../data/m4-2026-07-03/README.md)
