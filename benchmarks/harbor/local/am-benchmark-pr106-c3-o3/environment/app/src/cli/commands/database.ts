import { parseOptionSet, rejectExtraPositionals } from '../args.js';
import { registerCommand } from '../commands.js';
import { CliError, invalidUsage, messageForError } from '../errors.js';
import { writeHuman, writeJson } from '../output.js';

export function registerDatabaseCommands(): void {
  registerCommand({
    name: 'database backup',
    group: 'Data Commands',
    summary: 'Create and validate a closed SQLite backup',
    usage: 'database backup --output <absolute-path> [--replace] [--json]',
    examples: [
      'database backup --output /private/path/agentmonitor.db',
      'database backup --output /private/path/agentmonitor.db --replace --json',
    ],
    async handler(ctx, args) {
      const parsed = parseOptionSet(args, new Set(['--output']), new Set(['--replace']));
      rejectExtraPositionals(parsed.positionals, 'amon database backup --output <absolute-path> [--replace]');
      const output = parsed.values.get('--output');
      if (!output) throw invalidUsage('Missing required --output path.');

      const { resolveDbPath } = await import('../../db-path.js');
      const { createValidatedDatabaseBackup, DatabaseBackupPolicyError } = await import('../../db/backup.js');
      try {
        const result = await createValidatedDatabaseBackup({
          source: resolveDbPath(process.env),
          output,
          replace: parsed.flags.has('--replace'),
        });
        if (ctx.global.json) {
          writeJson(ctx, result);
          return;
        }
        writeHuman(
          ctx,
          [
            'Database backup created',
            `  output: ${result.output}`,
            `  bytes: ${result.bytes}`,
            `  journal_mode: ${result.journal_mode}`,
            `  integrity_check: ${result.integrity_check}`,
            `  foreign_key_violations: ${result.foreign_key_violations}`,
            `  replaced: ${result.replaced}`,
          ].join('\n'),
        );
      } catch (error) {
        if (error instanceof DatabaseBackupPolicyError) throw invalidUsage(error.message);
        throw new CliError(`Database backup failed: ${messageForError(error)}`);
      }
    },
  });
}
