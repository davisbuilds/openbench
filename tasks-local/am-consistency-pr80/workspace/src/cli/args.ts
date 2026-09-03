import path from 'node:path';
import { invalidUsage } from './errors.js';

export interface GlobalOptions {
  help: boolean;
  version: boolean;
  dbPath?: string;
  url?: string;
  json: boolean;
  plain: boolean;
  quiet: boolean;
  verbose: boolean;
  noColor: boolean;
  noInput: boolean;
}

export interface ParsedCli {
  commandName: string;
  global: GlobalOptions;
  args: string[];
}

export interface ParsedOptionSet {
  flags: Set<string>;
  values: Map<string, string>;
  positionals: string[];
}

const GLOBAL_VALUE_FLAGS = new Set(['--db-path', '--url']);
const GLOBAL_BOOLEAN_FLAGS = new Set([
  '--help',
  '-h',
  '--version',
  '--json',
  '--plain',
  '--quiet',
  '-q',
  '--verbose',
  '-v',
  '--no-color',
  '--no-input',
]);

function splitFlagAssignment(arg: string): [string, string | undefined] {
  if (!arg.startsWith('--')) return [arg, undefined];
  const eq = arg.indexOf('=');
  if (eq === -1) return [arg, undefined];
  return [arg.slice(0, eq), arg.slice(eq + 1)];
}

function defaultGlobalOptions(): GlobalOptions {
  return {
    help: false,
    version: false,
    json: false,
    plain: false,
    quiet: false,
    verbose: false,
    noColor: false,
    noInput: false,
  };
}

function setGlobalBoolean(global: GlobalOptions, flag: string): void {
  switch (flag) {
    case '--help':
    case '-h':
      global.help = true;
      break;
    case '--version':
      global.version = true;
      break;
    case '--json':
      global.json = true;
      break;
    case '--plain':
      global.plain = true;
      break;
    case '--quiet':
    case '-q':
      global.quiet = true;
      break;
    case '--verbose':
    case '-v':
      global.verbose = true;
      break;
    case '--no-color':
      global.noColor = true;
      break;
    case '--no-input':
      global.noInput = true;
      break;
  }
}

function setGlobalValue(global: GlobalOptions, flag: string, value: string): void {
  switch (flag) {
    case '--db-path':
      global.dbPath = value;
      break;
    case '--url':
      global.url = value.replace(/\/+$/, '');
      break;
  }
}

export function parseCli(argv: string[]): ParsedCli {
  const executableName = path.basename(argv[1] ?? 'amon');
  const commandName = executableName === 'cli.js' || executableName === 'cli.ts'
    ? 'amon'
    : executableName;
  const global = defaultGlobalOptions();
  const remaining: string[] = [];

  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i] as string;
    if (arg === '--') {
      if (i === 2) continue;
      remaining.push(...argv.slice(i + 1));
      break;
    }

    const [flag, inlineValue] = splitFlagAssignment(arg);
    if (GLOBAL_VALUE_FLAGS.has(flag)) {
      const value = inlineValue ?? argv[++i];
      if (!value) throw invalidUsage(`Missing value for ${flag}`);
      setGlobalValue(global, flag, value);
      continue;
    }
    if (GLOBAL_BOOLEAN_FLAGS.has(arg)) {
      setGlobalBoolean(global, arg);
      continue;
    }
    remaining.push(arg);
  }

  return { commandName, global, args: remaining };
}

export function parseOptionSet(args: string[], valueFlags: Set<string>, booleanFlags: Set<string>): ParsedOptionSet {
  const flags = new Set<string>();
  const values = new Map<string, string>();
  const positionals: string[] = [];

  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i] as string;
    if (arg === '--') {
      positionals.push(...args.slice(i + 1));
      break;
    }
    if (!arg.startsWith('-')) {
      positionals.push(arg);
      continue;
    }

    const [flag, inlineValue] = splitFlagAssignment(arg);
    if (valueFlags.has(flag)) {
      const value = inlineValue ?? args[++i];
      if (!value) throw invalidUsage(`Missing value for ${flag}`);
      values.set(flag, value);
      continue;
    }
    if (booleanFlags.has(arg) || booleanFlags.has(flag)) {
      flags.add(flag);
      continue;
    }

    throw invalidUsage(`Unknown option: ${arg}`);
  }

  return { flags, values, positionals };
}

export function parseIntegerOption(value: string | undefined, flag: string): number | undefined {
  if (value == null) return undefined;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) throw invalidUsage(`Invalid ${flag}: ${value}`);
  return parsed;
}

export function parseDateOption(value: string | undefined, flag: string): string | undefined {
  if (!value) return undefined;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) throw invalidUsage(`Invalid ${flag}: ${value}`);
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : parsed.toISOString();
}

export function requireOne(args: string[], usage: string): string {
  if (args.length !== 1) throw invalidUsage(`Usage: ${usage}`);
  return args[0] as string;
}

export function rejectExtraPositionals(args: string[], usage: string): void {
  if (args.length > 0) throw invalidUsage(`Usage: ${usage}`);
}
