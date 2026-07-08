# Token parity probe: DeepSeek V4 Flash

Branch: `token-parity-probe`
Probe date: 2026-07-08
Model: `deepseek-v4-flash`
Bridge: separate LiteLLM proxy on `127.0.0.1:4242` (production `:4141` was not touched)

## Normalized target schema

Per benchmark run, adapters should emit:

```json
{
  "tokens_input_uncached": 0,
  "tokens_cache_read": 0,
  "tokens_cache_write": 0,
  "tokens_output": 0,
  "tokens_reasoning": 0,
  "usage_raw": {},
  "token_basis": "vendor_split"
}
```

`token_basis` enum meanings:

- `vendor_split`: CLI exposes enough vendor-derived split fields to fill the schema.
- `combined_only`: only an aggregate total is available.
- `cache_inclusive`: an input/total field includes cache reads and must not be compared as fresh tokens without adjustment.
- `harness_reported`: harness reports its own estimate or transformed usage with no vendor split.
- `estimated`: adapter computed an estimate from non-usage data.

For DeepSeek V4 Flash, all four probed lanes expose vendor-derived split fields, but the fields are not named or shaped consistently.

## Vendor usage invariant observed

Direct DeepSeek `/chat/completions` sample (`.worker/proofs/logs/deepseek-direct-usage.json`) returned:

```json
{
  "usage": {
    "prompt_tokens": 7,
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 7,
    "prompt_tokens_details": {"cached_tokens": 0},
    "completion_tokens": 20,
    "completion_tokens_details": {"reasoning_tokens": 17},
    "total_tokens": 27
  }
}
```

Observed DeepSeek arithmetic:

- `prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens`.
- LiteLLM normalizes `prompt_cache_hit_tokens` to `prompt_tokens_details.cached_tokens`.
- `tokens_input_uncached = prompt_tokens - cached_tokens` (or DeepSeek-native `prompt_cache_miss_tokens` when present).
- `tokens_cache_read = cached_tokens` / `prompt_cache_hit_tokens`.
- `tokens_cache_write = 0` for DeepSeek V4 Flash in these probes; no write/create field appeared.
- `completion_tokens` is **reasoning-inclusive**: `completion_tokens = visible_text/tool-call tokens + reasoning_tokens`.
- `total_tokens = prompt_tokens + completion_tokens`; therefore it is cache-inclusive on the input side and reasoning-inclusive on the output side.
- `reasoning_content` appears on assistant messages; usage exposes only the count at `completion_tokens_details.reasoning_tokens`.

## Probe commands

The probes used isolated work directories and isolated HOME/config where supported. A custom LiteLLM callback wrote bridge-side usage JSONL; an excerpt is committed at `.worker/proofs/logs/bridge-usage-excerpt.jsonl` and duplicated for tests at `bench/tests/fixtures/usage/deepseek-v4-flash/bridge-usage-excerpt.jsonl`.

Start bridge on `:4242`:

```bash
source ~/.openbench/keys.env
export PYTHONPATH="$PWD/.worker/proofs:$PWD/bench/bridge${PYTHONPATH:+:$PYTHONPATH}"
export OPENBENCH_USAGE_LOG="$PWD/.worker/proofs/logs/bridge-usage.jsonl"
~/.openbench/bridge-venv/bin/litellm \
  --config .worker/proofs/bridge_config_probe.yaml \
  --host 127.0.0.1 --port 4242 --detailed_debug
```

The four concrete commands are reproduced by `bench/tools/parity_probe.py --run`; examples:

```bash
python3 bench/tools/parity_probe.py --harness pi --run \
  --stream .worker/proofs/fixtures/pi-stream.txt \
  --bridge-usage-log .worker/proofs/logs/bridge-usage.jsonl \
  --bridge-call-type acompletion

python3 bench/tools/parity_probe.py --harness opencode --run \
  --stream .worker/proofs/fixtures/opencode-stream.txt \
  --bridge-usage-log .worker/proofs/logs/bridge-usage.jsonl \
  --bridge-call-type acompletion

python3 bench/tools/parity_probe.py --harness claude --run \
  --stream .worker/proofs/fixtures/claude-stream.txt \
  --bridge-usage-log .worker/proofs/logs/bridge-usage.jsonl \
  --bridge-call-type anthropic_messages

python3 bench/tools/parity_probe.py --harness codex --run \
  --stream .worker/proofs/fixtures/codex-stream.txt \
  --bridge-usage-log .worker/proofs/logs/bridge-usage.jsonl \
  --bridge-call-type aresponses
```

## Per-harness findings

Numbers below are the normalized sums over the captured run. `output` is normalized to DeepSeek `completion_tokens` (reasoning-inclusive); `reasoning` is separately reported as the subset count.

| Harness | CLI surface | CLI normalized `{uncached, cache_read, cache_write, output, reasoning}` | Bridge/vendor normalized | Discrepancy | Proposed `token_basis` |
|---|---|---:|---:|---:|---|
| `pi` | JSONL; per-turn `turn_end.message.usage` with `input`, `cacheRead`, `cacheWrite`, `output`, `reasoning`, `totalTokens` | `{284, 4608, 0, 150, 43}` | `{284, 4608, 0, 150, 43}` | all zero | `vendor_split` |
| `opencode` | JSONL; per-step `step_finish.part.tokens` with `input`, `output`, `reasoning`, `cache.read`, `cache.write`, `total` | `{247, 21888, 0, 197, 43}` | main agent calls `{247, 21888, 0, 197, 43}` | all zero for main agent calls | `vendor_split` |
| `claude` | Single JSON result; top-level `usage`, plus `modelUsage` cumulative map | top-level `usage`: `{284, 0, 0, 266, unknown}`; `modelUsage`: `{651, 0, 0, 368, unknown}` | all Anthropic-route calls `{651, 0, 0, 368, 353}` | `modelUsage` matches input/output/cache but no reasoning split; top-level omits the session-title call | `vendor_split` for I/O/cache, with `tokens_reasoning=null` unless CLI adds details |
| `codex` | JSONL; final `turn.completed.usage` with `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens` | `{350, 55808, 0, 158, 46}` | `{350, 55808, 0, 158, 46}` | all zero | `vendor_split` |

### pi

Fixture: `.worker/proofs/fixtures/pi-stream.txt`
Clean test copy: `bench/tests/fixtures/usage/deepseek-v4-flash/pi-stream.txt`

Surface:

- `--mode json` emits JSONL.
- Every model round ends with `turn_end`; the assistant message carries `usage`.
- Fields are already split as `input`, `cacheRead`, `cacheWrite`, `output`, `reasoning`, `totalTokens`.

Mapping:

```text
tokens_input_uncached = sum(turn_end.message.usage.input)
tokens_cache_read     = sum(turn_end.message.usage.cacheRead)
tokens_cache_write    = sum(turn_end.message.usage.cacheWrite)
tokens_output         = sum(turn_end.message.usage.output)
tokens_reasoning      = sum(turn_end.message.usage.reasoning)
usage_raw             = list of turn_end.message.usage
token_basis           = vendor_split
```

Invariant check: `input + cacheRead + cacheWrite + output == totalTokens` for each captured pi turn. `reasoning` is a subset of `output`, not an add-on.

Current adapter issue: `bench/adapters/pi.py` currently returns one scalar `tokens = sum(input + output)` and drops cache reads, cache writes, and reasoning. It should keep that scalar only as a derived legacy field, if needed, after emitting the split schema.

### opencode

Fixture: `.worker/proofs/fixtures/opencode-stream.txt`
Clean test copy: `bench/tests/fixtures/usage/deepseek-v4-flash/opencode-stream.txt`

Surface:

- `--format json` emits JSONL.
- Every model round ends with `step_finish`; the token payload is `part.tokens`.
- Fields: `input`, `output`, `reasoning`, `cache.write`, `cache.read`, `total`.
- In this DeepSeek route, `output` is visible/non-reasoning output and `reasoning` is separate; DeepSeek/vendor `completion_tokens` equals `output + reasoning`.
- opencode also made an extra title-generation model call visible in the bridge log but not in the CLI stream fixture. Exclude non-agent/title calls when comparing adapter output to bridge ground truth.

Mapping:

```text
tokens_input_uncached = sum(step_finish.part.tokens.input)
tokens_cache_read     = sum(step_finish.part.tokens.cache.read)
tokens_cache_write    = sum(step_finish.part.tokens.cache.write)
tokens_output         = sum(output + reasoning)  # normalize to vendor completion_tokens
tokens_reasoning      = sum(reasoning)
usage_raw             = list of step_finish.part.tokens
token_basis           = vendor_split
```

Invariant check: `total = input + cache.read + cache.write + output + reasoning`. Reasoning is separate in opencode's surface but a subset of vendor completion tokens.

Current adapter issue: `bench/adapters/opencode.py` currently computes `tokens = input + output + reasoning` and excludes cache reads. That scalar mixes fresh input and reasoning-inclusive output, but loses the split fields. The rewrite should emit the split schema from `step_finish.part.tokens`.

### claude

Fixture: `.worker/proofs/fixtures/claude-stream.txt`
Clean test copy: `bench/tests/fixtures/usage/deepseek-v4-flash/claude-stream.txt`

Surface:

- `--output-format json` emits one result object.
- Top-level `usage` has Anthropic-style fields: `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`.
- `modelUsage` is a cumulative per-model camelCase map: `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `costUSD`, etc.
- In this probe, `modelUsage.deepseek-v4-flash` matched the bridge's aggregate input/output/cache across the actual request plus Claude Code's title request. The top-level `usage` matched only the final user-facing request.
- Claude Code did **not** surface `completion_tokens_details.reasoning_tokens` in either top-level `usage` or `modelUsage`; bridge usage showed 353 DeepSeek reasoning tokens across the two Anthropic-route calls.

Mapping:

```text
# Prefer cumulative modelUsage for run totals.
tokens_input_uncached = sum(modelUsage[*].inputTokens)
tokens_cache_read     = sum(modelUsage[*].cacheReadInputTokens)
tokens_cache_write    = sum(modelUsage[*].cacheCreationInputTokens)
tokens_output         = sum(modelUsage[*].outputTokens)
tokens_reasoning      = null  # not exposed by Claude Code JSON today
usage_raw             = {"usage": top-level usage, "modelUsage": modelUsage}
token_basis           = vendor_split  # I/O/cache are split; reasoning unavailable in CLI
```

Invariant check: Anthropic-style `input_tokens`/`inputTokens` exclude cache reads; cache read and cache creation fields are disjoint. For DeepSeek behind Claude Code, `outputTokens` is reasoning-inclusive, but Claude Code does not expose the reasoning subset.

Current adapter issue: `bench/adapters/claude.py` already prefers `modelUsage` for its scalar. The rewrite should keep that preference for split I/O/cache, add raw usage, and set `tokens_reasoning` to `None`/missing rather than `0` when no reasoning field is present.

Open concern: In the captured DeepSeek Anthropic-compatible run, Claude Code did not perform filesystem tools; it returned a short result. This does not invalidate the usage-surface finding, but a future re-probe should use a CLI/model combination that definitely exercises tools if Claude Code's DeepSeek tool handling changes.

### codex

Fixture: `.worker/proofs/fixtures/codex-stream.txt`
Clean test copy: `bench/tests/fixtures/usage/deepseek-v4-flash/codex-stream.txt`

Surface:

- `--json` emits JSONL.
- The final `turn.completed` carries one aggregate usage object for the whole run.
- Fields: `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`.
- `input_tokens` is cache-inclusive; `cached_input_tokens` must be subtracted to get uncached input.
- `output_tokens` is reasoning-inclusive for this DeepSeek bridge route; `reasoning_output_tokens` is the reasoning subset.

Mapping:

```text
tokens_input_uncached = usage.input_tokens - usage.cached_input_tokens
tokens_cache_read     = usage.cached_input_tokens
tokens_cache_write    = 0  # no field exposed in this route
tokens_output         = usage.output_tokens
tokens_reasoning      = usage.reasoning_output_tokens
usage_raw             = turn.completed.usage
token_basis           = vendor_split
```

Invariant check: `input_tokens = uncached + cached_input_tokens`; `output_tokens` includes `reasoning_output_tokens`. Current adapter bug: `bench/adapters/codex.py` computes `tokens = (input_tokens - cached_input_tokens) + output_tokens + reasoning_output_tokens`, double-counting reasoning because `output_tokens` already includes it.

## Biggest discrepancy found

The largest adapter-design discrepancy is codex's legacy scalar arithmetic: for the captured run it would report `350 + 158 + 46 = 554` fresh tokens, while the comparable fresh-token basis is `350 + 158 = 508`; reasoning was double-counted by 46 tokens (about 9.1% of the fresh comparable count). Across harnesses, comparing only one scalar is much worse: cache-inclusive totals range from about 5k (pi) to 56k (codex) mostly because of different prompt/tool scaffolding and cache behavior, not because one agent did equivalent work with 10x tokens.

## Proposed adapter rewrite plan

1. Extend the run result schema to include the split fields plus `usage_raw` and `token_basis`.
2. Preserve the old `tokens` scalar temporarily as a derived compatibility value, but make reports prefer split fields.
3. Use `tokens_fresh_total = tokens_input_uncached + tokens_cache_write + tokens_output` for a cache-read-excluded comparable total. Do **not** add `tokens_reasoning` again when `tokens_output` is vendor completion/output tokens.
4. Add per-adapter parsers backed by the committed fixtures under `bench/tests/fixtures/usage/deepseek-v4-flash/`.
5. For Claude Code, represent unavailable reasoning as `null`/missing, not `0`, unless a future CLI exposes it.

## Open questions

- Claude Code: is there a flag or future JSON schema that exposes DeepSeek `reasoning_tokens`? Bridge ground truth sees it, but the CLI result does not.
- opencode: should title-generation/background calls be disabled for benchmark probes, or explicitly excluded from bridge comparisons? CLI stream excludes the title call, so adapter output should be based on `step_finish` only.
- DeepSeek cache writes: no cache-write/create count appeared; if other vendors expose writes, schema should keep the field disjoint from uncached input.
- Reporting UX: decide whether published benchmark tables show cache-read-excluded fresh totals, cache reads as a separate column, or both.

## Artifacts

- Scrubbed captured CLI usage streams: `.worker/proofs/fixtures/{pi,opencode,claude,codex}-stream.txt` (usage-bearing records only; prompt/output/reasoning text removed).
- Bridge usage excerpt: `.worker/proofs/logs/bridge-usage-excerpt.jsonl`.
- Direct DeepSeek usage schema sample: `.worker/proofs/logs/deepseek-direct-usage.json`.
- Clean test fixtures: `bench/tests/fixtures/usage/deepseek-v4-flash/` (same usage-only scrubbed shape).
- Verification harness: `bench/tools/parity_probe.py`.

Cleanup: the probe bridge on `:4242` was killed after verification.
