import type { GlobalOptions } from './args.js';

export interface CliIO {
  stdout: NodeJS.WritableStream;
  stderr: NodeJS.WritableStream;
}

export interface CliContext {
  commandName: string;
  global: GlobalOptions;
  io: CliIO;
}

export function writeStdout(ctx: CliContext, text: string): void {
  ctx.io.stdout.write(text);
  if (!text.endsWith('\n')) ctx.io.stdout.write('\n');
}

export function writeStderr(ctx: CliContext, text: string): void {
  ctx.io.stderr.write(text);
  if (!text.endsWith('\n')) ctx.io.stderr.write('\n');
}

export function writeJson(ctx: CliContext, value: unknown): void {
  writeStdout(ctx, `${JSON.stringify(value, null, 2)}\n`);
}

export function writeHuman(ctx: CliContext, text: string): void {
  if (ctx.global.quiet) return;
  writeStdout(ctx, text);
}

// eslint-disable-next-line no-control-regex
const ANSI_ESCAPE_RE = /\u001b\[[0-9;?]*[ -/]*[@-~]/g;
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;

export function sanitizeTerminal(value: unknown): string {
  return String(value ?? '')
    .replace(ANSI_ESCAPE_RE, '')
    .replace(CONTROL_RE, '');
}

export function formatTable(rows: string[][]): string {
  if (rows.length === 0) return '';
  const widths = rows[0]!.map((_, col) => Math.max(...rows.map(row => (row[col] ?? '').length)));
  return rows
    .map(row => row.map((cell, col) => cell.padEnd(widths[col] ?? 0)).join('  ').trimEnd())
    .join('\n');
}
