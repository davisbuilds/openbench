import { Router, type Request, type Response } from 'express';
import { insertEvent } from '../db/queries.js';
import { broadcaster } from '../sse/emitter.js';
import { coerceJsonLikeBody } from './json-body.js';
import {
  parseOtelLogs,
  parseOtelMetrics,
  type OtelLogsPayload,
  type OtelMetricsPayload,
} from '../otel/parser.js';
import { safelyMaintainTraceSummaryForEvent } from '../trace-quality/service.js';

export const otelRouter = Router();

// Content-Type guard: JSON only (415 for protobuf)
function requireJson(req: Request, res: Response): boolean {
  const ct = req.headers['content-type'] ?? '';
  if (ct.includes('application/x-protobuf') || ct.includes('application/protobuf')) {
    res.status(415).json({
      error: 'Protobuf not supported yet. Use JSON format.',
      hint: 'Set OTEL_EXPORTER_OTLP_PROTOCOL=http/json',
    });
    return false;
  }
  return true;
}

// POST /api/otel/v1/logs
otelRouter.post('/v1/logs', (req: Request, res: Response) => {
  if (!requireJson(req, res)) return;

  const payload = coerceJsonLikeBody(req.body);
  if (!payload || typeof payload !== 'object') {
    res.status(400).json({ error: 'Invalid OTEL JSON payload' });
    return;
  }
  const events = parseOtelLogs(payload as OtelLogsPayload);

  for (const event of events) {
    const row = insertEvent(event);
    if (row) {
      broadcaster.broadcast('event', row as unknown as Record<string, unknown>);
      safelyMaintainTraceSummaryForEvent(row.id, 'otel log ingest');
    }
  }

  // OTLP-compliant: empty object = success
  res.status(200).json({});
});

// POST /api/otel/v1/metrics
otelRouter.post('/v1/metrics', (req: Request, res: Response) => {
  if (!requireJson(req, res)) return;

  const payload = coerceJsonLikeBody(req.body);
  if (!payload || typeof payload !== 'object') {
    res.status(400).json({ error: 'Invalid OTEL JSON payload' });
    return;
  }
  const deltas = parseOtelMetrics(payload as OtelMetricsPayload);

  for (const delta of deltas) {
    // Emit a synthetic llm_response event carrying the metric delta values.
    // This lets the existing event pipeline aggregate tokens/cost per session.
    const hasTokens = delta.tokens_in_delta > 0 || delta.tokens_out_delta > 0
      || delta.cache_read_delta > 0 || delta.cache_write_delta > 0;
    const hasCost = delta.cost_usd_delta > 0;

    if (!hasTokens && !hasCost) continue;

    const row = insertEvent({
      session_id: delta.session_id,
      agent_type: delta.agent_type,
      event_type: 'llm_response',
      status: 'success',
      tokens_in: delta.tokens_in_delta,
      tokens_out: delta.tokens_out_delta,
      cache_read_tokens: delta.cache_read_delta,
      cache_write_tokens: delta.cache_write_delta,
      cost_usd: hasCost ? delta.cost_usd_delta : undefined,
      model: delta.model,
      metadata: { _synthetic: true, _source: 'otel_metric' },
      source: 'otel',
    });

    if (row) {
      broadcaster.broadcast('event', row as unknown as Record<string, unknown>);
      safelyMaintainTraceSummaryForEvent(row.id, 'otel metric ingest');
    }
  }

  res.status(200).json({});
});

// POST /api/otel/v1/traces — stub for future implementation
otelRouter.post('/v1/traces', (req: Request, res: Response) => {
  if (!requireJson(req, res)) return;
  const payload = coerceJsonLikeBody(req.body);
  if (!payload || typeof payload !== 'object') {
    res.status(400).json({ error: 'Invalid OTEL JSON payload' });
    return;
  }

  // Accept and acknowledge traces but don't process them yet
  res.status(200).json({});
});
