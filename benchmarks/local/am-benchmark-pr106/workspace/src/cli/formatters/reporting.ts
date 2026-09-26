import { formatTable, sanitizeTerminal } from '../output.js';

export function formatCurrency(value: number | null | undefined): string {
  return `$${(value ?? 0).toFixed(4)}`;
}

export function formatUsageSummary(summary: { total_cost_usd: number; total_usage_events: number; total_sessions: number; total_input_tokens: number; total_output_tokens: number }): string {
  return [
    `Cost: ${formatCurrency(summary.total_cost_usd)}`,
    `Usage events: ${summary.total_usage_events}`,
    `Sessions: ${summary.total_sessions}`,
    `Tokens: ${summary.total_input_tokens} in / ${summary.total_output_tokens} out`,
  ].join('\n');
}

export function formatRows(rows: Array<Record<string, unknown>>, columns: string[]): string {
  if (rows.length === 0) return '(no rows)';
  return formatTable([
    columns.map(column => column.toUpperCase()),
    ...rows.map(row => columns.map(column => sanitizeTerminal(row[column] ?? '-'))),
  ]);
}
