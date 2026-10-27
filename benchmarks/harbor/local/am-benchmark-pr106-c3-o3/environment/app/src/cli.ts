#!/usr/bin/env node
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parseCli } from './cli/args.js';
import { dispatchCommand, listCommands } from './cli/commands.js';
import { CliError, EXIT_SUCCESS, exitCodeForError, messageForError } from './cli/errors.js';
import { rootHelp } from './cli/help.js';
import { packageVersion } from './cli/package.js';
import { registerAllCommands } from './cli/register.js';
import { type CliContext, writeStderr, writeStdout } from './cli/output.js';

export interface MainResult {
  exitCode: number;
}

export async function main(argv: string[] = process.argv, io = {
  stdout: process.stdout,
  stderr: process.stderr,
}): Promise<MainResult> {
  registerAllCommands();

  try {
    const parsed = parseCli(argv);
    const ctx: CliContext = {
      commandName: parsed.commandName,
      global: parsed.global,
      io,
    };

    if (parsed.global.version) {
      writeStdout(ctx, packageVersion());
      return { exitCode: EXIT_SUCCESS };
    }
    if (parsed.global.help && parsed.args.length === 0) {
      writeStdout(ctx, rootHelp(parsed.commandName, listCommands()));
      return { exitCode: EXIT_SUCCESS };
    }
    if (parsed.args.length === 0) {
      writeStdout(ctx, rootHelp(parsed.commandName, listCommands()));
      return { exitCode: EXIT_SUCCESS };
    }

    await dispatchCommand(ctx, parsed.args);
    return { exitCode: EXIT_SUCCESS };
  } catch (error) {
    const parsed = (() => {
      try {
        return parseCli(argv);
      } catch {
        return null;
      }
    })();
    const ctx: CliContext = {
      commandName: parsed?.commandName ?? 'amon',
      global: parsed?.global ?? {
        help: false,
        version: false,
        json: false,
        plain: false,
        quiet: false,
        verbose: false,
        noColor: false,
        noInput: false,
      },
      io,
    };
    const prefix = error instanceof CliError ? 'error' : 'unexpected error';
    writeStderr(ctx, `${prefix}: ${messageForError(error)}`);
    if (!(error instanceof CliError) && ctx.global.verbose && error instanceof Error && error.stack) {
      writeStderr(ctx, error.stack);
    }
    return { exitCode: exitCodeForError(error) };
  }
}

function isEntrypoint(argvPath: string | undefined): boolean {
  if (!argvPath) return false;
  try {
    return fs.realpathSync(argvPath) === fs.realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return import.meta.url === new URL(argvPath, 'file:').href;
  }
}

if (isEntrypoint(process.argv[1])) {
  const result = await main();
  process.exitCode = result.exitCode;
}
