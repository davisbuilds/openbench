import { readFileSync } from 'node:fs';
import path from 'node:path';
import { backfillBenchmarkCost, backfillBenchmarkStudy, insertEvent } from '../db/queries.js';
import { pricingRegistry } from '../pricing/index.js';

/**
 * Importer for openbench `results.jsonl` benchmark runs.
 *
 * Each row is one benchmark cell — a single `codex exec` / harness invocation
 * against one task+model+trial — carrying trusted per-cell token accounting.
 * openbench runs the harness in an ephemeral `CODEX_HOME` that is `rmtree`d after
 * each cell, so there is no session JSONL or OTEL stream for the live/import
 * paths to see; this batch importer is the only way that accounting reaches the
 * console. Rows map to one aggregate `llm_response` event apiece, tagged
 * `source='benchmark'` so they are segregated from real-activity aggregates.
 */

export interface BenchmarkImportOptions {
  dryRun?: boolean;
  /**
   * Manual study override (escape hatch / legacy grouping). Sets both the grouping
   * key and the display slug to this label, overriding the row's own identity.
   */
  study?: string;
}

export interface BenchmarkImportResult {
  file: string;
  rowsRead: number;
  eventsImported: number;
  duplicates: number;
  /** Duplicate cells whose previously-null cost was backfilled from a now-resolved price. */
  costsBackfilled: number;
  skipped: number;
  /** Distinct bench model strings that resolved to no price (billed as null). */
  unpricedModels: string[];
}

interface BenchmarkRow {
  run_id?: unknown;
  harness?: unknown;
  model?: unknown;
  task?: unknown;
  trial?: unknown;
  ts_iso?: unknown;
  success?: unknown;
  completed?: unknown;
  error?: unknown;
  failure_class?: unknown;
  failure_reason?: unknown;
  wall_time_s?: unknown;
  t_agent_s?: unknown;
  turns?: unknown;
  tokens_input_uncached?: unknown;
  tokens_output?: unknown;
  tokens_cache_read?: unknown;
  tokens_cache_write?: unknown;
  tokens_reasoning?: unknown;
  token_basis?: unknown;
  usage_evidence_grade?: unknown;
  cost_usd?: unknown;
  cost_source?: unknown;
  harness_version?: unknown;
  score?: unknown;
  workspace_changed?: unknown;
  // Identity fields emitted by openbench (feat/results-row-study-identity).
  // Preferred over amon's legacy derivations when present.
  canonical_model?: unknown;
  reasoning_effort?: unknown;
  is_open_model?: unknown;
  study?: unknown;
  study_sha256?: unknown;
  suite?: unknown;
}

/**
 * Split a glued codex model string into `(canonical, effort)`. Legacy fallback
 * only — openbench now emits `canonical_model` + `reasoning_effort` directly.
 */
function splitEffort(model: string): { canonical: string; effort: string | undefined } {
  const lastDash = model.lastIndexOf('-');
  if (lastDash > 0) {
    const suffix = model.slice(lastDash + 1);
    if (EFFORT_SUFFIXES.includes(suffix)) {
      return { canonical: model.slice(0, lastDash), effort: suffix };
    }
  }
  return { canonical: model, effort: undefined };
}

// Reasoning-effort suffixes openbench appends to a codex model string
// (`gpt-5.6-terra-xhigh` ran `-m gpt-5.6-terra -c model_reasoning_effort=xhigh`).
// They are settings, not price tiers, so strip them for pricing lookup only —
// the event keeps the faithful bench string.
const EFFORT_SUFFIXES = ['xhigh', 'high', 'medium', 'low', 'minimal', 'max'];

function num(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function str(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

/**
 * Cost for one cell, preferring the row's own captured `cost_usd` (OpenRouter's
 * `usage.cost` / the bridge's `x-litellm-response-cost`, authoritative when
 * present) and otherwise deriving from the pricing tables. Returns null when the
 * model is unpriced, so callers can surface it loudly rather than bill it as $0.
 * Mirrors `_forks/openbench/experiments/analyze_cost.py:effective_row_cost`.
 */
function resolveBenchmarkCost(row: BenchmarkRow): number | null {
  if (typeof row.cost_usd === 'number' && Number.isFinite(row.cost_usd)) {
    return row.cost_usd;
  }
  const model = str(row.model);
  if (!model) return null;
  // reasoning is billed at the output rate (vendor convention); the vendor split
  // already folds reasoning into tokens_output, so do not add it again.
  const tokens = {
    input: num(row.tokens_input_uncached),
    output: num(row.tokens_output),
    cacheRead: num(row.tokens_cache_read),
    cacheWrite: num(row.tokens_cache_write),
  };
  const direct = pricingRegistry.calculate(model, tokens);
  if (direct !== null) return direct;

  // Retry against the base model with an effort suffix stripped.
  const lastDash = model.lastIndexOf('-');
  if (lastDash > 0 && EFFORT_SUFFIXES.includes(model.slice(lastDash + 1))) {
    return pricingRegistry.calculate(model.slice(0, lastDash), tokens);
  }
  return null;
}

export function importBenchmarkResults(
  filePath: string,
  options: BenchmarkImportOptions = {},
): BenchmarkImportResult {
  const result: BenchmarkImportResult = {
    file: filePath,
    rowsRead: 0,
    eventsImported: 0,
    duplicates: 0,
    costsBackfilled: 0,
    skipped: 0,
    unpricedModels: [],
  };
  const unpriced = new Set<string>();

  // Legacy study fallback for pre-field rows: the results.jsonl's parent dir name
  // (e.g. `am-consistency-pareto-2026-08-29`), else the file's own basename.
  const legacyStudy = path.basename(path.dirname(filePath))
    || path.basename(filePath).replace(/\.[^.]*$/, '');

  const contents = readFileSync(filePath, 'utf-8');
  for (const line of contents.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    result.rowsRead += 1;

    let row: BenchmarkRow;
    try {
      const parsed: unknown = JSON.parse(trimmed);
      // Valid JSON that is not a plain object (`null`, an array, a bare scalar)
      // would throw on the first property access and abort the whole file after
      // earlier rows have committed. Reject it as a skip instead.
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        result.skipped += 1;
        continue;
      }
      row = parsed as BenchmarkRow;
    } catch {
      result.skipped += 1;
      continue;
    }

    const runId = str(row.run_id);
    const harness = str(row.harness);
    const model = str(row.model);
    if (!runId || !harness || !model) {
      result.skipped += 1;
      continue;
    }

    const cost = resolveBenchmarkCost(row);
    if (cost === null) unpriced.add(model);

    // Study identity: prefer the manual override, then openbench's own fields,
    // then the legacy parent-dir fallback. study_id (= study_sha256) is the exact
    // per-run grouping key; study is the human slug label.
    const studyId = options.study ?? str(row.study_sha256) ?? legacyStudy;
    const study = options.study ?? str(row.study) ?? legacyStudy;

    // Model identity: prefer openbench's split, else strip the effort suffix.
    const split = splitEffort(model);
    const canonicalModel = str(row.canonical_model) ?? split.canonical;
    const reasoningEffort = str(row.reasoning_effort) ?? split.effort ?? null;
    const isOpenModel = typeof row.is_open_model === 'boolean' ? row.is_open_model : null;

    if (options.dryRun) {
      // Count would-be inserts without touching the DB. Duplicate detection is
      // not available in dry-run; report everything as importable.
      result.eventsImported += 1;
      continue;
    }

    const durationMs = Math.round(num(row.wall_time_s || row.t_agent_s) * 1000) || undefined;
    const isError = row.success === false || (typeof row.error === 'string' && row.error.length > 0);

    const event = insertEvent({
      event_id: runId,
      session_id: runId,
      agent_type: harness,
      event_type: 'llm_response',
      status: isError ? 'error' : 'success',
      project: str(row.task),
      model,
      tokens_in: num(row.tokens_input_uncached),
      tokens_out: num(row.tokens_output),
      cache_read_tokens: num(row.tokens_cache_read),
      cache_write_tokens: num(row.tokens_cache_write),
      cost_usd: cost,
      source: 'benchmark',
      study_id: studyId,
      study,
      duration_ms: durationMs,
      client_timestamp: str(row.ts_iso),
      metadata: {
        run_id: runId,
        task: str(row.task),
        trial: row.trial,
        harness,
        harness_version: str(row.harness_version),
        score: row.score,
        success: row.success,
        completed: row.completed,
        failure_class: str(row.failure_class),
        failure_reason: str(row.failure_reason),
        turns: row.turns,
        tokens_reasoning: num(row.tokens_reasoning),
        token_basis: str(row.token_basis),
        usage_evidence_grade: str(row.usage_evidence_grade),
        cost_source: str(row.cost_source),
        // success with no workspace change = a no-op trial (honesty flag).
        workspace_changed: typeof row.workspace_changed === 'boolean' ? row.workspace_changed : null,
        // Arm/model identity (study_id/study live in their own columns).
        canonical_model: canonicalModel,
        reasoning_effort: reasoningEffort,
        is_open_model: isOpenModel,
        suite: str(row.suite) ?? null,
      },
    });

    if (event) {
      result.eventsImported += 1;
    } else {
      result.duplicates += 1;
      // A re-import after rates/identity were added: the cell already exists (so
      // insert is skipped) but its stored cost/study may still be null. Backfill
      // the now-resolved values so usage stops summing $0 and the row joins its
      // study.
      if (cost !== null && backfillBenchmarkCost(runId, cost)) {
        result.costsBackfilled += 1;
      }
      backfillBenchmarkStudy(runId, studyId, study);
    }
  }

  result.unpricedModels = [...unpriced].sort();
  return result;
}
