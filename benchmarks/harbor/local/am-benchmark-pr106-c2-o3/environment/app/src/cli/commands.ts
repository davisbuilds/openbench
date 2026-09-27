import type { GlobalOptions } from './args.js';
import { invalidUsage } from './errors.js';
import { commandHelp } from './help.js';
import type { CliContext } from './output.js';
import { writeStdout } from './output.js';

export type CommandHandler = (ctx: CliContext, args: string[]) => Promise<void> | void;

export interface Command {
  name: string;
  group: string;
  summary: string;
  usage?: string;
  examples?: string[];
  handler: CommandHandler;
}

const commands: Command[] = [];

export function registerCommand(command: Command): void {
  commands.push(command);
}

export function listCommands(): Command[] {
  return [...commands];
}

function findCommand(args: string[]): { command: Command; rest: string[] } | null {
  for (let size = Math.min(3, args.length); size >= 1; size -= 1) {
    const candidate = args.slice(0, size).join(' ');
    const command = commands.find(item => item.name === candidate);
    if (command) return { command, rest: args.slice(size) };
  }
  return null;
}

function applyGlobalEnv(global: GlobalOptions): void {
  if (global.dbPath) process.env.AGENTMONITOR_DB_PATH = global.dbPath;
}

export async function dispatchCommand(ctx: CliContext, args: string[]): Promise<void> {
  const found = findCommand(args);
  if (!found) {
    const attempted = args[0] ?? '';
    throw invalidUsage(attempted ? `Unknown command: ${attempted}` : 'Missing command');
  }
  if (ctx.global.help) {
    writeStdout(
      ctx,
      commandHelp(
        ctx.commandName,
        found.command.usage ?? found.command.name,
        found.command.summary,
        found.command.examples,
      ),
    );
    return;
  }
  applyGlobalEnv(ctx.global);
  await found.command.handler(ctx, found.rest);
}
