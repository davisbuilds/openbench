import { parseIntegerOption, parseOptionSet } from '../args.js';
import { registerCommand } from '../commands.js';
import { invalidUsage } from '../errors.js';
import { formatCurrency, formatRows, formatUsageSummary } from '../formatters/reporting.js';
import { writeJson, writeStdout } from '../output.js';
import type { CliContext } from '../output.js';

function parseNumberOption(value: string | undefined, flag: string): number | undefined {
  if (value == null) return undefined;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw invalidUsage(`Invalid ${flag}: ${value}`);
  return parsed;
}

function commonParams(args: string[]) {
  const parsed = parseOptionSet(
    args,
    new Set([
      '--date-from',
      '--date-to',
      '--project',
      '--agent',
      '--model',
      '--provider',
      '--tier',
      '--limit',
      '--offset',
      '--session-id',
      '--status',
      '--observation-type',
      '--tool',
      '--tool-name',
      '--score-name',
      '--min-score',
      '--max-score',
      '--kind',
      '--severity',
      '--trace-id',
      '--observation-id',
      '--target-type',
    ]),
    new Set(['--exclude-low-coverage']),
  );
  if (parsed.positionals.length > 0) throw invalidUsage(`Unexpected argument: ${parsed.positionals[0]}`);
  return {
    date_from: parsed.values.get('--date-from'),
    date_to: parsed.values.get('--date-to'),
    project: parsed.values.get('--project'),
    agent: parsed.values.get('--agent'),
    model: parsed.values.get('--model'),
    provider: parsed.values.get('--provider'),
    tier: parsed.values.get('--tier'),
    limit: parseIntegerOption(parsed.values.get('--limit'), '--limit'),
    offset: parseIntegerOption(parsed.values.get('--offset'), '--offset'),
    session_id: parsed.values.get('--session-id'),
    status: parsed.values.get('--status'),
    observation_type: parsed.values.get('--observation-type'),
    tool: parsed.values.get('--tool'),
    tool_name: parsed.values.get('--tool-name'),
    score_name: parsed.values.get('--score-name'),
    min_score: parseNumberOption(parsed.values.get('--min-score'), '--min-score'),
    max_score: parseNumberOption(parsed.values.get('--max-score'), '--max-score'),
    kind: parsed.values.get('--kind'),
    severity: parsed.values.get('--severity'),
    trace_id: parsed.values.get('--trace-id'),
    observation_id: parsed.values.get('--observation-id'),
    target_type: parsed.values.get('--target-type'),
    exclude_low_coverage: parsed.flags.has('--exclude-low-coverage'),
  };
}

async function initDb() {
  const { initSchema } = await import('../../db/schema.js');
  const { closeDb } = await import('../../db/connection.js');
  initSchema();
  return { closeDb };
}

function writeReport(ctx: CliContext, value: unknown, human: string): void {
  if (ctx.global.json) writeJson(ctx, value);
  else writeStdout(ctx, human);
}

export function registerReportingCommands(): void {
  registerCommand({
    name: 'usage summary',
    group: 'Usage Commands',
    summary: 'Show usage cost and token totals',
    usage: 'usage summary [--date-from <date>] [--project <name>] [--json]',
    async handler(ctx, args) {
      const params = commonParams(args);
      const { closeDb } = await initDb();
      try {
        const { getUsageSummary } = await import('../../db/v2-queries.js');
        const summary = getUsageSummary(params);
        writeReport(ctx, summary, formatUsageSummary(summary));
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'usage daily',
    group: 'Usage Commands',
    summary: 'Show daily usage costs',
    usage: 'usage daily [--date-from <date>] [--date-to <date>] [--json]',
    async handler(ctx, args) {
      const params = commonParams(args);
      const { closeDb } = await initDb();
      try {
        const { getUsageCoverage, getUsageDaily } = await import('../../db/v2-queries.js');
        const payload = { data: getUsageDaily(params), coverage: getUsageCoverage(params) };
        writeReport(ctx, payload, formatRows(payload.data as unknown as Array<Record<string, unknown>>, ['date', 'cost_usd', 'usage_events', 'session_count']));
      } finally {
        closeDb();
      }
    },
  });

  for (const [name, summary, getter, columns] of [
    ['usage models', 'Show usage grouped by model', 'getUsageModels', ['model', 'provider', 'tier', 'cost_usd', 'usage_events']],
    ['usage projects', 'Show usage grouped by project', 'getUsageProjects', ['project', 'cost_usd', 'usage_events', 'session_count']],
  ] as const) {
    registerCommand({
      name,
      group: 'Usage Commands',
      summary,
      usage: `${name} [--date-from <date>] [--json]`,
      async handler(ctx, args) {
        const params = commonParams(args);
        const { closeDb } = await initDb();
        try {
          const queries = await import('../../db/v2-queries.js');
          const data = queries[getter](params);
          const coverage = queries.getUsageCoverage(params);
          const payload = { data, coverage };
          writeReport(ctx, payload, formatRows(data as unknown as Array<Record<string, unknown>>, columns as unknown as string[]));
        } finally {
          closeDb();
        }
      },
    });
  }

  registerCommand({
    name: 'usage statusline',
    group: 'Usage Commands',
    summary: 'Print a one-line cost summary',
    usage: 'usage statusline [--plain]',
    async handler(ctx, args) {
      const params = commonParams(args);
      const { closeDb } = await initDb();
      try {
        const { getUsageSummary } = await import('../../db/v2-queries.js');
        const summary = getUsageSummary(params);
        const line = ctx.global.plain ? `${formatCurrency(summary.total_cost_usd)} today` : `AgentMonitor ${formatCurrency(summary.total_cost_usd)} usage`;
        writeStdout(ctx, line);
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'usage budgets',
    group: 'Usage Commands',
    summary: 'Show read-only usage budget state',
    usage: 'usage budgets [--json]',
    async handler(ctx, args) {
      commonParams(args);
      const { getUsageBudgets } = await import('../../usage/budgets.js');
      const budgets = getUsageBudgets();
      writeReport(ctx, budgets, formatRows(budgets.data as unknown as Array<Record<string, unknown>>, ['name', 'spent_usd', 'limit_usd', 'state']));
    },
  });

  registerCommand({
    name: 'usage tier-feedback',
    group: 'Usage Commands',
    summary: 'Show advisory model-tier feedback',
    usage: 'usage tier-feedback [--date-from <date>] [--json]',
    async handler(ctx, args) {
      const params = commonParams(args);
      const { closeDb } = await initDb();
      try {
        const { getUsageTierFeedback } = await import('../../usage/tier-feedback.js');
        const report = getUsageTierFeedback(params);
        writeReport(ctx, report, `Tier feedback: ${report.tier_mismatches.length} mismatch(es), ${report.cost_outliers.length} cost outlier(s)`);
      } finally {
        closeDb();
      }
    },
  });

  for (const [name, summary, getter, columns] of [
    ['analytics summary', 'Show historical session analytics summary', 'getAnalyticsSummary', ['total_sessions', 'total_messages', 'total_user_messages']],
    ['analytics tools', 'Show tool analytics', 'getAnalyticsTools', ['tool_name', 'category', 'count']],
    ['analytics top-sessions', 'Show top historical sessions', 'getAnalyticsTopSessions', ['id', 'project', 'agent', 'message_count', 'tool_call_count']],
  ] as const) {
    registerCommand({
      name,
      group: 'Analytics Commands',
      summary,
      usage: `${name} [--date-from <date>] [--json]`,
      async handler(ctx, args) {
        const params = commonParams(args);
        const { closeDb } = await initDb();
        try {
          const queries = await import('../../db/v2-queries.js');
          const data = queries[getter](params);
          const payload = Array.isArray(data) ? { data, coverage: queries.getAnalyticsCoverage(params, name === 'analytics tools' ? 'tool_analytics_capable' : 'all_sessions') } : data;
          const human = Array.isArray(data)
            ? formatRows(data as unknown as Array<Record<string, unknown>>, columns as unknown as string[])
            : formatRows([data as unknown as Record<string, unknown>], columns as unknown as string[]);
          writeReport(ctx, payload, human);
        } finally {
          closeDb();
        }
      },
    });
  }

  registerCommand({
    name: 'quality traces',
    group: 'Quality Commands',
    summary: 'List trace-quality traces (one per session, from the lean summary)',
    usage: 'quality traces [--session-id <id>] [--limit <n>] [--json]',
    async handler(ctx, args) {
      const params = commonParams(args);
      const { closeDb } = await initDb();
      try {
        const { ensureSessionTraceSummaryBackfill } = await import('../../trace-quality/summary.js');
        const { listSessionTraces } = await import('../../trace-quality/on-demand.js');
        // The CLI runs out-of-band from the server, so self-heal the summary here
        // too — otherwise an upgraded DB with events but no summary rows reports an
        // empty list until the server has run its startup backfill.
        ensureSessionTraceSummaryBackfill();
        const result = listSessionTraces(params);
        writeReport(ctx, result, formatRows(result.data as unknown as Array<Record<string, unknown>>, ['id', 'session_id', 'agent_type', 'status']));
      } finally {
        closeDb();
      }
    },
  });
}
