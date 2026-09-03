function usageMetricPresenceCondition(alias: string): string {
  return `(
    COALESCE(${alias}.cost_usd, 0) > 0
    OR COALESCE(${alias}.tokens_in, 0) > 0
    OR COALESCE(${alias}.tokens_out, 0) > 0
    OR COALESCE(${alias}.cache_read_tokens, 0) > 0
    OR COALESCE(${alias}.cache_write_tokens, 0) > 0
  )`;
}

function usageTimestampExpression(alias: string): string {
  return `datetime(COALESCE(${alias}.client_timestamp, ${alias}.created_at))`;
}

function overlappingCodexOtelUsageCondition(alias: string): string {
  return `(
    ${alias}.agent_type = 'codex'
    AND ${alias}.source = 'otel'
    AND ${usageMetricPresenceCondition(alias)}
    AND EXISTS (
      SELECT 1
      FROM events imported_usage
      WHERE imported_usage.session_id = ${alias}.session_id
        AND imported_usage.agent_type = 'codex'
        AND imported_usage.source = 'import'
        AND ${usageMetricPresenceCondition('imported_usage')}
        AND ${usageTimestampExpression('imported_usage')} >= ${usageTimestampExpression(alias)}
    )
  )`;
}

export function excludeOverlappingCodexOtelUsageCondition(alias: string): string {
  return `NOT ${overlappingCodexOtelUsageCondition(alias)}`;
}

export function reconciledUsageSum(alias: string, column: string): string {
  return `COALESCE(SUM(CASE WHEN ${overlappingCodexOtelUsageCondition(alias)} THEN 0 ELSE COALESCE(${alias}.${column}, 0) END), 0)`;
}
