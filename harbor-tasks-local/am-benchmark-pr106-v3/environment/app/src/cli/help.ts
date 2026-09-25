import type { Command } from './commands.js';

export function rootHelp(commandName: string, commands: Command[]): string {
  const groups = new Map<string, Command[]>();
  for (const command of commands) {
    const existing = groups.get(command.group) ?? [];
    existing.push(command);
    groups.set(command.group, existing);
  }

  const lines = [
    `Usage: ${commandName} [global flags] <command> [args]`,
    '',
    'Local AgentMonitor runtime and data browser.',
    '',
    'Examples:',
    `  ${commandName} serve`,
    `  ${commandName} import --source codex --dry-run`,
    `  ${commandName} sessions list --agent codex --json`,
    `  ${commandName} usage statusline`,
    '',
  ];

  for (const [group, grouped] of groups) {
    lines.push(`${group}:`);
    for (const command of grouped) {
      lines.push(`  ${command.name.padEnd(24)} ${command.summary}`);
    }
    lines.push('');
  }

  lines.push(
    'Global flags:',
    '  -h, --help                 Show help',
    '  --version                  Show package version',
    '  --db-path <path>           Override AGENTMONITOR_DB_PATH for this run',
    '  --url <url>                HTTP target for server-backed commands',
    '  --json                     Emit JSON to stdout',
    '  --plain                    Emit stable plain text where supported',
    '  -q, --quiet                Suppress non-essential human output',
    '  -v, --verbose              Emit extra diagnostics to stderr',
    '  --no-color                 Disable ANSI color',
    '  --no-input                 Disable prompts',
    '',
    `Both '${commandName}' and '${commandName === 'amon' ? 'agentmonitor' : 'amon'}' run this CLI.`,
  );

  return `${lines.join('\n')}\n`;
}

export function commandHelp(commandName: string, usage: string, summary: string, examples: string[] = []): string {
  const lines = [
    `Usage: ${commandName} ${usage}`,
    '',
    summary,
  ];
  if (examples.length > 0) {
    lines.push('', 'Examples:', ...examples.map(example => `  ${commandName} ${example}`));
  }
  return `${lines.join('\n')}\n`;
}
