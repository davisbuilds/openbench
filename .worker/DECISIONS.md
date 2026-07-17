# Subscription bridge decisions

- Use CLIProxyAPI's OpenAI chat-completions ingress for the primary `grokbuild` × `gpt-5.6` lane; do not add any OAuth implementation to OpenBench.
- Keep the existing canonical model name `gpt-5.6` and replace its former pay-per-token route, because the adopted subscription bridge is now the intended engine.
- Put the OpenBench counting proxy in front of CLIProxyAPI. This preserves response usage capture while leaving OAuth injection and refresh entirely downstream in CLIProxyAPI.
- Use `CLIPROXYAPI_BASE_URL` for the non-secret endpoint and optional `CLIPROXYAPI_API_KEY` only for CLIProxyAPI ingress access control. When ingress access control is disabled, provide a non-secret placeholder solely because Grok requires an `env_key`.
- Do not implement the Claude stretch lane. Claude Code's existing `--bare` behavior is an intentional API-key billing/isolation boundary; changing it is unnecessary for the primary target and would broaden risk.
- The smoke script owns one primary lane and enforces one metered HTTP model request. It is manual-only and exits with setup guidance before invoking a harness when the daemon is unavailable.

# Upstream CLI version check decisions

- Treat npm’s `latest` dist-tag as the authoritative upstream version and query package metadata directly with Python’s standard-library HTTP client; `--check-upstream` never invokes npm or installs anything.
- Keep Cursor explicitly manual because it is not npm-distributed and no authoritative registry endpoint is part of the existing pin metadata.
- Maintain at most one open issue with the exact requested title. A clean run does not auto-close it, preserving human ownership of review and remediation.
