# Subscription-auth bridge

OpenBench uses [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) rather than a custom OAuth service. CLIProxyAPI owns Codex/ChatGPT subscription login, token refresh, and auth injection; OpenBench only selects its local OpenAI-compatible ingress.

## Setup

```sh
brew install cliproxyapi
# Configure subscription OAuth in:
# /opt/homebrew/etc/cliproxyapi.conf
# Then run the daemon on localhost:8317.
```

The primary supported cell is `grokbuild` × `gpt-5.6`. Its isolated Grok config declares a `[model."gpt-5.6"]` chat-completions provider at `http://127.0.0.1:8317/v1`. Override that non-secret address with `CLIPROXYAPI_BASE_URL`. If CLIProxyAPI ingress authentication is enabled, export `CLIPROXYAPI_API_KEY`; otherwise the adapter supplies a harmless local placeholder required by Grok's provider schema.

`OPENAI_API_KEY` is not a dependency of this route. The adapter filters it by name without retrieving its value and excludes it from the Grok child environment even if the parent shell has one. Subscription OAuth remains in CLIProxyAPI's auth store and is never copied into the benchmark workspace or harness environment.

## Metering topology

The counting proxy sits **in front of** CLIProxyAPI:

```text
Grok Build
  -> OpenBench counting proxy /cell/<opaque>/subbridge/v1/chat/completions
  -> CLIProxyAPI http://127.0.0.1:8317/v1/chat/completions
  -> Codex/ChatGPT subscription upstream
```

This position lets `obench/proxy.py` observe the OpenAI-compatible response usage while CLIProxyAPI still performs auth injection and refresh downstream. Run a benchmark cell with metering using:

```sh
obench run --harness grokbuild --model gpt-5.6 \
  --task make-it-run --trials 1 --proxy
```

For Docker cells, the adapter defaults to `host.docker.internal:8317`; the counting proxy itself remains host-side. CLIProxyAPI must be reachable from the selected execution mode.

## Manual smoke

```sh
bench/smoke_subbridge.sh
```

The script first checks that CLIProxyAPI and Grok Build are installed and that the daemon port is reachable. If setup is missing it exits without a model request and prints setup instructions. Otherwise it unsets `OPENAI_API_KEY`, sends exactly one tiny prompt through the Grok adapter and counting proxy, and requires exactly one successful metered request. This is the only intended live subscription smoke; unit tests use local mock HTTP servers.

The Claude-harness stretch lane is intentionally not enabled: Claude Code's `--bare` mode is an API-key billing boundary, while CLIProxyAPI's Anthropic ingress is meant for clients that can safely select that endpoint without weakening the existing Claude adapter isolation contract.
