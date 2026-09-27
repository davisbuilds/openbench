import { randomUUID } from 'node:crypto';

import { parseDateOption, parseIntegerOption, parseOptionSet } from '../args.js';
import { registerCommand } from '../commands.js';
import { invalidUsage, unavailable } from '../errors.js';
import { writeJson, writeStderr, writeStdout } from '../output.js';
import type { CliContext } from '../output.js';

function parseWarehousePublishArgs(args: string[]) {
  const parsed = parseOptionSet(
    args,
    new Set(['--date-from', '--date-to', '--account', '--min-batch']),
    new Set(['--dry-run', '--json']),
  );
  if (parsed.positionals.length > 0) throw invalidUsage(`Unexpected argument: ${parsed.positionals[0]}`);
  const minBatch = parseIntegerOption(parsed.values.get('--min-batch'), '--min-batch') ?? 0;
  if (minBatch < 0) throw invalidUsage('--min-batch must be >= 0');
  return {
    dryRun: parsed.flags.has('--dry-run'),
    json: parsed.flags.has('--json'),
    dateFrom: parseDateOption(parsed.values.get('--date-from'), '--date-from'),
    dateTo: parseDateOption(parsed.values.get('--date-to'), '--date-to'),
    account: parsed.values.get('--account'),
    minBatch,
  };
}

async function initDb() {
  const { initSchema } = await import('../../db/schema.js');
  const { closeDb } = await import('../../db/connection.js');
  initSchema();
  return { closeDb };
}

function withSuppressedConsoleLog<T>(suppress: boolean, fn: () => T): T {
  if (!suppress) return fn();
  const original = console.log;
  console.log = () => undefined;
  try {
    return fn();
  } finally {
    console.log = original;
  }
}

function writeWarehouseResult(ctx: CliContext, value: unknown, human: string, json: boolean): void {
  if (ctx.global.json || json) writeJson(ctx, value);
  else writeStdout(ctx, human);
}

export function registerWarehouseCommands(): void {
  registerCommand({
    name: 'warehouse publish',
    group: 'Warehouse Commands',
    summary: 'Publish content-free session trace summaries to the shared warehouse',
    usage: 'warehouse publish [--dry-run] [--date-from <date>] [--date-to <date>] [--account <label>] [--min-batch <n>] [--json]',
    examples: [
      'warehouse publish --dry-run --json',
      'warehouse publish --date-from 2026-06-01 --date-to 2026-06-30',
    ],
    async handler(ctx, args) {
      const options = parseWarehousePublishArgs(args);
      const emitJson = ctx.global.json || options.json;
      const { closeDb } = await initDb();
      try {
        const { config } = await import('../../config.js');
        const { ensureSessionTraceSummaryBackfill } = await import('../../trace-quality/summary.js');
        const { listWarehouseSessionTraceSummaries } = await import('../../warehouse/source.js');
        const {
          mapSummaryToRunRow,
          assertContentFree,
          applyMinBatch,
          buildLineage,
        } = await import('../../warehouse/runs-export.js');
        const { planRuns, publishRuns } = await import('../../warehouse/postgres-sink.js');

        if (!options.dryRun && !config.warehouse.dsn) {
          throw unavailable('set AGENTMONITOR_WAREHOUSE_DSN to publish warehouse rows');
        }

        const account = options.account ?? config.warehouse.account;
        if (options.account) {
          writeStderr(
            ctx,
            `warning: --account overrides AGENTMONITOR_WAREHOUSE_ACCOUNT; re-publishing the same sessions under a different account can double-count`,
          );
        }

        withSuppressedConsoleLog(emitJson, () => ensureSessionTraceSummaryBackfill());
        const runId = `amon-${randomUUID()}`;
        const summaries = listWarehouseSessionTraceSummaries({
          date_from: options.dateFrom,
          date_to: options.dateTo,
        });
        const rows = summaries.map(summary => mapSummaryToRunRow(summary, account, runId));
        for (const row of rows) {
          assertContentFree(row);
        }
        const batch = applyMinBatch(rows, options.minBatch);
        const lineage = buildLineage({
          runId,
          account,
          windowStart: options.dateFrom ?? null,
          windowEnd: options.dateTo ?? null,
          sessionsPublished: batch.published.length,
          sessionsSuppressed: batch.suppressed.length,
          minBatch: options.minBatch,
          grantRole: config.warehouse.biRole,
        });

        if (options.dryRun) {
          const plan = planRuns(batch.published, lineage, config.warehouse);
          writeWarehouseResult(
            ctx,
            {
              dry_run: true,
              run_id: plan.run_id,
              schema: plan.schema,
              account,
              rows_planned: plan.rows_published,
              rows_suppressed: batch.suppressed.length,
              min_batch: options.minBatch,
              grant_role: plan.grant_role,
              grant_skipped: plan.grant_skipped,
              statements: plan.statements,
            },
            [
              `Dry run: ${plan.rows_published} row(s) planned, ${batch.suppressed.length} suppressed.`,
              `Schema: ${plan.schema}`,
              `Run: ${plan.run_id}`,
              'SQL plan:',
              ...plan.statements.map(statement => `  ${statement}`),
            ].join('\n'),
            options.json,
          );
          return;
        }

        const result = await publishRuns(batch.published, lineage, config.warehouse);
        writeWarehouseResult(
          ctx,
          {
            dry_run: false,
            run_id: result.run_id,
            account,
            rows_published: result.rows_published,
            rows_suppressed: batch.suppressed.length,
            grant_role: result.grant_role,
            grant_skipped: result.grant_skipped,
          },
          `Published ${result.rows_published} warehouse row(s); ${batch.suppressed.length} suppressed. Run: ${result.run_id}`,
          options.json,
        );
      } finally {
        closeDb();
      }
    },
  });
}
