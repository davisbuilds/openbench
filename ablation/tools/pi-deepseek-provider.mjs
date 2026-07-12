export default function (pi) {
  pi.registerProvider("deepseek-bridge", {
    name: "DeepSeek via OpenBench capture proxy",
    baseUrl: "http://127.0.0.1:4142/v1",
    apiKey: "$DEEPSEEK_API_KEY",
    api: "openai-completions",
    models: [{
      id: "deepseek-v4-flash",
      name: "deepseek-v4-flash",
      reasoning: true,
      input: ["text"],
      compat: { supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false, thinkingFormat: "deepseek", requiresReasoningContentOnAssistantMessages: true },
      thinkingLevelMap: { off: null },
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 128000,
      maxTokens: 8192
    }]
  });
}
