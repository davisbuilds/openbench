# COUNTING-PROXY Feasibility Spike

Date: 2026-07-13
Branch: `counting-proxy-spike`
Scope: codex, pi, claude, opencode only. cursor/devin/grok intentionally skipped.

## Verdict table

| Lane | Verdict | Routing tested | Auth survived? | Usage capture |
|---|---:|---|---:|---|
| codex x subscription | WORKS | `openai_base_url=http://127.0.0.1:<port>/backend-api/codex`, proxy upstream `https://chatgpt.com` | Yes | Yes: terminal Codex SSE event has `response.usage`; CLI also emitted `turn.completed.usage`. |
| pi x subscription (`openai-codex`) | WORKS | isolated HOME `models.json` override for `openai-codex.baseUrl=http://127.0.0.1:<port>/backend-api`, proxy upstream `https://chatgpt.com`, `transport=sse` | Yes | Yes: final pi event carries assistant `usage`; same Codex SSE terminal shape is proxy-parseable. |
| claude x anthropic-compat | WORKS | `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>/anthropic`, proxy upstream `https://api.deepseek.com`, `ANTHROPIC_API_KEY=$DEEPSEEK_API_KEY` | Yes | Yes: proxy logged Anthropic usage including cache fields. |
| opencode x deepseek chat | WORKS | custom opencode provider `baseURL=http://127.0.0.1:<port>`, proxy upstream `https://api.deepseek.com` | Yes | Yes: proxy logged OpenAI chat `usage`. |

## Key empirical details

- codex first-party subscription endpoint is the ChatGPT backend: `https://chatgpt.com/backend-api/codex` for `/models` and `/responses`.
- `codex` accepts `-c openai_base_url=...`; using `https://api.openai.com` with ChatGPT OAuth fails with 401/missing API scopes, but using `https://chatgpt.com/backend-api/codex` works.
- The minimal proxy does not implement WebSocket upgrade. codex first tries WebSocket (`GET /backend-api/codex/responses`), receives 405 through the proxy, then falls back to HTTPS/SSE and completes. Counting proxy should either support WebSocket or force/expect SSE fallback.
- pi can be pointed at localhost without modifying bench code by isolated `~/.pi/agent/models.json` provider override. Setting `transport=sse` avoids WebSocket behavior for this spike.

## Captured usage samples

Auth headers were redacted in proxy logs to first 8 chars + length.

### codex x subscription

Proxy evidence: `POST /backend-api/codex/responses` returned 200 with `Authorization: Bearer <redacted>`; CLI completed `OK`.

Proxy-captured usage object from terminal Codex SSE event (`response.usage`):

```json
{
  "input_tokens": 18650,
  "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 4992},
  "output_tokens": 16,
  "output_tokens_details": {"reasoning_tokens": 9},
  "total_tokens": 18666
}
```

### pi x subscription

Proxy evidence: `POST /backend-api/codex/responses` returned 200 with `Authorization: Bearer <redacted>`; CLI completed `OK`.

Proxy-captured usage object from terminal Codex SSE event (`response.usage`):

```json
{
  "input_tokens": 1051,
  "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
  "output_tokens": 5,
  "output_tokens_details": {"reasoning_tokens": 0},
  "total_tokens": 1056
}
```

Pi's final JSON event normalized the same usage to `input=1051`, `output=5`, `cacheRead=0`, `cacheWrite=0`, `reasoning=0`, `totalTokens=1056`.

### claude x DeepSeek Anthropic-compatible

Proxy evidence: `POST /anthropic/v1/messages?beta=true` returned 200 with `x-api-key: sk-<redacted>`.

Proxy-captured usage:

```json
{
  "input_tokens": 1381,
  "cache_creation_input_tokens": 0,
  "cache_read_input_tokens": 0,
  "output_tokens": 18,
  "service_tier": "standard"
}
```

### opencode x DeepSeek chat

Proxy evidence: `POST /chat/completions` returned 200 with `Authorization: Bearer <redacted>`.

Proxy-captured usage:

```json
{
  "prompt_tokens": 11310,
  "completion_tokens": 13,
  "total_tokens": 11323,
  "prompt_tokens_details": {"cached_tokens": 0},
  "completion_tokens_details": {"reasoning_tokens": 10},
  "prompt_cache_hit_tokens": 0,
  "prompt_cache_miss_tokens": 11310
}
```

## Build / no-build recommendation

Build the counting proxy, with caveats:

1. Implement the proxy as pass-through only; no dialect translation is needed for these lanes.
2. Support both JSON and SSE terminal usage extraction. Codex/pi usage is on the terminal `response.completed` SSE event under `response.usage`.
3. Decide WebSocket strategy for codex/pi: either support WebSocket proxying or configure/force SSE. The spike proves auth survives over SSE fallback, but pure HTTP-only proxying adds failed WebSocket attempts for codex unless disabled.
4. Preserve auth headers exactly, but never log their values beyond first 8 chars + length.
5. Keep provider-specific usage schemas intact in the logs; normalize later outside the transparent proxy.

## Verification commands run

- Proxy unit smoke: local JSON, SSE, and gzip responses through `.worker/spike_proxy.py`; asserted usage parsing and auth redaction.
- Live probes: one tiny `Reply exactly OK` request per lane, plus failed endpoint-discovery attempts for codex/pi. No bench code was modified.
