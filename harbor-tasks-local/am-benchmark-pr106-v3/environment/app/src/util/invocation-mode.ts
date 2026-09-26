// Invocation mode: whether an agent session was driven interactively (a human at
// a TUI/editor) or headlessly (a one-shot programmatic run: `claude -p` / `codex exec`).
//
// The distinction is captured only for Claude Code and Codex, whose session logs
// carry a reliable discriminator. Antigravity/Gemini does not emit an equivalent
// signal, so those sessions have no mode and render without a pill.

export type InvocationMode = 'interactive' | 'headless';

// Claude Code writes `entrypoint` on every transcript line and `promptSource` on
// user turns. Headless `claude -p` runs report `entrypoint: "sdk-cli"` with
// `promptSource: "sdk"`; interactive TUI runs report `entrypoint: "cli"`.
export function claudeInvocationMode(
  entrypoint: string | undefined,
  promptSource: string | undefined,
): InvocationMode | undefined {
  if (entrypoint === 'sdk-cli' || promptSource === 'sdk') return 'headless';
  if (entrypoint === 'cli') return 'interactive';
  return undefined;
}

// Codex writes `originator` in its session_meta record. `codex exec` (headless)
// reports `codex_exec`; the interactive surfaces report `codex-tui`,
// `codex_cli_rs`, or `Codex Desktop`.
export function codexInvocationMode(originator: string | undefined): InvocationMode | undefined {
  if (originator === 'codex_exec') return 'headless';
  if (originator === 'codex-tui' || originator === 'codex_cli_rs' || originator === 'Codex Desktop') {
    return 'interactive';
  }
  return undefined;
}
