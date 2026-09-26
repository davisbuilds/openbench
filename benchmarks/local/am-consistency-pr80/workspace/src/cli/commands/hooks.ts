import { spawn } from 'node:child_process';
import path from 'node:path';
import readline from 'node:readline/promises';
import { parseOptionSet } from '../args.js';
import { registerCommand } from '../commands.js';
import { invalidUsage } from '../errors.js';
import { effectiveBaseUrl } from '../http.js';
import { findPackageRoot } from '../package.js';
import { writeHuman, writeStdout } from '../output.js';

function codexConfig(url: string): string {
  return `[otel]
log_user_prompt = true

[otel.exporter.otlp-http]
endpoint = "${url}/api/otel/v1/logs"
protocol = "json"

[otel.metrics_exporter.otlp-http]
endpoint = "${url}/api/otel/v1/metrics"
protocol = "json"`;
}

async function confirmInstall(): Promise<boolean> {
  if (!process.stdin.isTTY) return false;
  const rl = readline.createInterface({ input: process.stdin, output: process.stderr });
  try {
    const answer = await rl.question('Install AgentMonitor Claude hooks into ~/.claude/settings.json? [y/N] ');
    return answer.trim().toLowerCase() === 'y' || answer.trim().toLowerCase() === 'yes';
  } finally {
    rl.close();
  }
}

function runScript(script: string, args: string[]): Promise<number> {
  return new Promise((resolve) => {
    const child = spawn(script, args, { stdio: 'inherit' });
    child.on('exit', code => resolve(code ?? 1));
    child.on('error', () => resolve(1));
  });
}

export function registerHookCommands(): void {
  registerCommand({
    name: 'hooks print-codex-config',
    group: 'Hook Commands',
    summary: 'Print Codex OTEL configuration for AgentMonitor',
    usage: 'hooks print-codex-config [--url <url>]',
    examples: ['hooks print-codex-config'],
    handler(ctx, args) {
      const parsed = parseOptionSet(args, new Set(), new Set());
      if (parsed.positionals.length > 0) throw invalidUsage('Usage: amon hooks print-codex-config [--url <url>]');
      writeStdout(ctx, codexConfig(effectiveBaseUrl(ctx.global.url)));
    },
  });

  registerCommand({
    name: 'hooks install claude',
    group: 'Hook Commands',
    summary: 'Install AgentMonitor Claude Code hooks',
    usage: 'hooks install claude [--dry-run] [--force] [--python] [--uninstall]',
    examples: ['hooks install claude --dry-run', 'hooks install claude --force'],
    async handler(ctx, args) {
      const parsed = parseOptionSet(args, new Set(), new Set(['--dry-run', '--force', '--python', '--uninstall']));
      if (parsed.positionals.length > 0) throw invalidUsage('Usage: amon hooks install claude [options]');
      const root = findPackageRoot();
      const script = path.join(root, 'hooks', 'claude-code', 'install.sh');
      const scriptArgs = ['--url', effectiveBaseUrl(ctx.global.url)];
      if (parsed.flags.has('--python')) scriptArgs.push('--python');
      if (parsed.flags.has('--uninstall')) scriptArgs.push('--uninstall');

      if (parsed.flags.has('--dry-run')) {
        writeStdout(ctx, [
          'Claude hook install dry run',
          `  script: ${script}`,
          `  args: ${scriptArgs.join(' ')}`,
          '  target: ~/.claude/settings.json',
        ].join('\n'));
        return;
      }

      if (!parsed.flags.has('--force')) {
        if (ctx.global.noInput) throw invalidUsage('hooks install claude requires --force when --no-input is set.');
        const confirmed = await confirmInstall();
        if (!confirmed) throw invalidUsage('Claude hook installation cancelled. Re-run with --force to skip confirmation.');
      }

      const code = await runScript(script, scriptArgs);
      if (code !== 0) throw new Error(`Claude hook installer exited with ${code}`);
      writeHuman(ctx, 'Claude hooks installed.');
    },
  });
}
