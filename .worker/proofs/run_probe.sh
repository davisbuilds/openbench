#!/usr/bin/env bash
set -euo pipefail
h="${1:-}"
case "$h" in
  pi|opencode|codex|claude) ;;
  *) echo "unknown harness: $h" >&2; exit 2;;
esac
ROOT="$(pwd)"
OUT="$ROOT/.worker/proofs/fixtures/${h}-stream.txt"
WD="$ROOT/.worker/proofs/work/${h}"
HOMEISO="$ROOT/.worker/proofs/home/${h}"
mkdir -p "$WD" "$HOMEISO" "$(dirname "$OUT")"
rm -rf "$WD" "$HOMEISO"
mkdir -p "$WD" "$HOMEISO"
PROMPT='In this directory: create hello.txt containing exactly hi, then read it back, then say done. Keep the final answer short.'
BASE_ENV=(env -i "PATH=$PATH" "HOME=$HOMEISO" "TMPDIR=${TMPDIR:-/tmp}" "SHELL=${SHELL:-/bin/sh}" "USER=${USER:-openbench}" "LOGNAME=${LOGNAME:-openbench}" "DEEPSEEK_API_KEY=openbench-bridge-placeholder")
case "$h" in
  pi)
    EXT="$HOMEISO/open-provider.mjs"
    cat > "$EXT" <<'JS'
export default function (pi) {
  pi.registerProvider("deepseek_probe", {
    name: "DeepSeek Probe Bridge",
    baseUrl: "http://127.0.0.1:4242/v1",
    apiKey: "$DEEPSEEK_API_KEY",
    api: "openai-completions",
    models: [{
      id: "deepseek-v4-flash", name: "deepseek-v4-flash",
      reasoning: true, input: ["text"],
      compat: {supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false, thinkingFormat: "deepseek", requiresReasoningContentOnAssistantMessages: true},
      thinkingLevelMap: {off: null},
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 128000, maxTokens: 8192
    }]
  });
}
JS
    (cd "$WD" && "${BASE_ENV[@]}" pi -p --no-extensions -e "$EXT" --provider deepseek_probe --model deepseek-v4-flash --thinking medium --mode json "$PROMPT") >"$OUT" 2>&1
    ;;
  opencode)
    CFG='{"provider":{"deepseek_probe":{"npm":"@ai-sdk/openai-compatible","name":"DeepSeek Probe Bridge","options":{"baseURL":"http://127.0.0.1:4242/v1","apiKey":"{env:DEEPSEEK_API_KEY}"},"models":{"deepseek-v4-flash":{}}}}}'
    (cd "$WD" && "${BASE_ENV[@]}" OPENCODE_CONFIG_CONTENT="$CFG" opencode run --dir "$WD" -m deepseek_probe/deepseek-v4-flash --variant medium --auto --format json "$PROMPT") >"$OUT" 2>&1
    ;;
  codex)
    (cd "$WD" && "${BASE_ENV[@]}" BENCH_BRIDGE_PORT=4242 codex exec --json --skip-git-repo-check -C "$WD" -s workspace-write -c 'model_providers.deepseek.name="DeepSeek Probe Bridge"' -c 'model_providers.deepseek.base_url="http://127.0.0.1:4242/v1"' -c 'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"' -c 'model_providers.deepseek.wire_api="responses"' -c 'model_provider="deepseek"' -c 'model_reasoning_effort="medium"' -m deepseek-v4-flash "$PROMPT") >"$OUT" 2>&1
    ;;
  claude)
    (cd "$WD" && "${BASE_ENV[@]}" CLAUDE_CONFIG_DIR="$HOMEISO/.claude" ANTHROPIC_BASE_URL="http://127.0.0.1:4242" ANTHROPIC_API_KEY="openbench-bridge-placeholder" DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 DISABLE_BUG_COMMAND=1 DISABLE_NON_ESSENTIAL_MODEL_CALLS=1 claude -p --bare --output-format json --model deepseek-v4-flash --effort medium --dangerously-skip-permissions --no-session-persistence "$PROMPT") >"$OUT" 2>&1
    ;;
  *) echo "unknown harness" >&2; exit 2;;
esac
