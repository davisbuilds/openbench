import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

// ─── Types ──────────────────────────────────────────────────────────────

interface PricingDataRates {
  inputCostPerMTok: number;
  outputCostPerMTok: number;
  cacheReadCostPerMTok: number;
  cacheWriteCostPerMTok: number;
}

// A higher prompt-size band: when a request's prompt exceeds `abovePromptTokens`,
// these rates replace the base rates for every token class (that is how Google's
// long-context tiering works — e.g. Gemini doubles all rates above 200K prompt).
interface PricingDataTier extends PricingDataRates {
  abovePromptTokens: number;
}

// A dated rate change: from `from` (inclusive ISO date) onward, these rates (and
// their own optional prompt-size tiers) replace the model's inline base rates.
// Used for promos that revert and provider price changes with a known date.
interface PricingDataSchedulePeriod extends PricingDataRates {
  from: string;
  tiers?: PricingDataTier[];
  /** Free-form provenance; ignored by the loader. */
  note?: string;
}

interface PricingDataModel extends PricingDataRates {
  aliases?: string[];
  deprecated: boolean;
  tiers?: PricingDataTier[];
  schedule?: PricingDataSchedulePeriod[];
}

interface PricingDataFile {
  provider: string;
  lastUpdated: string;
  models: Record<string, PricingDataModel>;
}

export interface PricingRates {
  inputCostPerToken: number;
  outputCostPerToken: number;
  cacheReadCostPerToken: number;
  cacheWriteCostPerToken: number;
}

export interface PricingTier extends PricingRates {
  abovePromptTokens: number;
}

/**
 * A dated rate period: from `from` (epoch ms; -Infinity for the base period)
 * onward, `rates` (tier-selected by `tiers` when present) are in force until a
 * later period supersedes them.
 */
export interface RatePeriod {
  from: number;
  rates: PricingRates;
  tiers?: PricingTier[];
}

export interface ModelPricing extends PricingRates {
  provider: string;
  deprecated: boolean;
  /** Higher prompt-size bands, ascending by threshold. Absent for flat models. */
  tiers?: PricingTier[];
  /**
   * Dated rate periods ascending by `from`; `periods[0]` is the base period
   * (from -Infinity) built from the inline rates. A model with no `schedule`
   * has exactly one period, so the date selection is a no-op for it.
   */
  periods: RatePeriod[];
}

export interface TokenCounts {
  input: number;
  output: number;
  cacheRead?: number;
  cacheWrite?: number;
}

export interface ResolvedModelPricing {
  canonicalModel: string;
  pricing: ModelPricing;
}

// ─── PricingRegistry ────────────────────────────────────────────────────

const M_TOK = 1_000_000;

function toPerToken(rates: PricingDataRates): PricingRates {
  return {
    inputCostPerToken: rates.inputCostPerMTok / M_TOK,
    outputCostPerToken: rates.outputCostPerMTok / M_TOK,
    cacheReadCostPerToken: rates.cacheReadCostPerMTok / M_TOK,
    cacheWriteCostPerToken: rates.cacheWriteCostPerMTok / M_TOK,
  };
}

// Convert prompt-size bands to per-token rates, ascending by threshold. Returns
// undefined for a flat model so callers can leave `tiers` unset.
function buildTiers(tiers?: PricingDataTier[]): PricingTier[] | undefined {
  if (!tiers || tiers.length === 0) return undefined;
  return tiers
    .map(tier => ({ ...toPerToken(tier), abovePromptTokens: tier.abovePromptTokens }))
    .sort((a, b) => a.abovePromptTokens - b.abovePromptTokens);
}

// Build the dated rate periods: the inline rates form the base period (from
// -Infinity), and each `schedule` entry adds a later period. Sorted ascending
// by `from`; entries with an unparseable `from` are dropped rather than
// silently mispricing every event from the epoch.
function buildPeriods(model: PricingDataModel, baseTiers?: PricingTier[]): RatePeriod[] {
  const base: RatePeriod = { from: -Infinity, rates: toPerToken(model), tiers: baseTiers };
  const scheduled: RatePeriod[] = (model.schedule ?? [])
    .map(period => ({ from: Date.parse(period.from), rates: toPerToken(period), tiers: buildTiers(period.tiers) }))
    .filter(period => Number.isFinite(period.from));
  return [base, ...scheduled].sort((a, b) => a.from - b.from);
}

/** A point in time to price at: a Date, epoch ms, or ISO string. */
export type PricingDate = Date | number | string;

// SQLite stores `created_at` as the zone-less form `YYYY-MM-DD HH:MM:SS` but the
// value is UTC (written by CURRENT_TIMESTAMP). Node's Date parses that separator
// form in the host's LOCAL timezone, so on a non-UTC install an event within the
// UTC offset of a rate boundary would land in the wrong period. Normalize the
// bare SQLite datetime to an explicit-UTC ISO string; anything already carrying a
// zone (client_timestamp is ISO with `Z`/offset) or not matching is left as-is.
const SQLITE_UTC = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}(?:\.\d+)?)$/;

export function normalizeSqliteTimestamp(value: string): string {
  const match = SQLITE_UTC.exec(value.trim());
  return match ? `${match[1]}T${match[2]}Z` : value;
}

// Resolve an `at` argument to epoch ms. Omitted or unparseable → now, so a
// missing/garbled event timestamp prices at the current period rather than
// silently jumping to a scheduled future rate.
function resolveAtMs(at?: PricingDate | null): number {
  if (at == null) return Date.now();
  if (at instanceof Date) return at.getTime();
  if (typeof at === 'number') return at;
  const ms = Date.parse(normalizeSqliteTimestamp(at));
  return Number.isFinite(ms) ? ms : Date.now();
}

// Pick the rate period in force at `atMs`: the latest period whose `from` is
// at or before that instant (boundaries are inclusive). `periods` is sorted
// ascending and `periods[0]` is from -Infinity, so this always resolves.
function selectPeriod(pricing: ModelPricing, atMs: number): RatePeriod {
  let period = pricing.periods[0];
  for (const candidate of pricing.periods) {
    if (candidate.from <= atMs) period = candidate;
  }
  return period;
}

// Pick the effective rates for a request. Selects the dated period first, then
// the prompt-size tier within it: flat periods use their base rates; tiered
// periods apply the highest band whose threshold the prompt strictly exceeds
// (boundaries exclusive). Prompt size = uncached input + cache reads + writes.
function selectRates(pricing: ModelPricing, tokens: TokenCounts, atMs: number): PricingRates {
  const period = selectPeriod(pricing, atMs);
  if (!period.tiers || period.tiers.length === 0) return period.rates;
  const promptTokens = tokens.input + (tokens.cacheRead ?? 0) + (tokens.cacheWrite ?? 0);
  let rates: PricingRates = period.rates;
  for (const tier of period.tiers) {
    if (promptTokens > tier.abovePromptTokens) rates = tier;
  }
  return rates;
}

export class PricingRegistry {
  private models = new Map<string, ModelPricing>();
  private aliases = new Map<string, string>(); // alias → canonical name

  constructor() {
    this.loadAll();
  }

  private loadAll(): void {
    const __dirname = path.dirname(fileURLToPath(import.meta.url));
    // Works in dev (tsx: src/pricing/) and prod (dist/pricing/ after copy)
    const dataDir = path.join(__dirname, 'data');

    for (const file of ['claude.json', 'codex.json', 'gemini.json', 'openrouter.json']) {
      try {
        const raw = readFileSync(path.join(dataDir, file), 'utf-8');
        const data = JSON.parse(raw) as PricingDataFile;
        this.loadProvider(data);
      } catch {
        // Data file missing or malformed — skip silently in production
      }
    }
  }

  private loadProvider(data: PricingDataFile): void {
    for (const [canonicalName, model] of Object.entries(data.models)) {
      const baseTiers = buildTiers(model.tiers);
      const pricing: ModelPricing = {
        ...toPerToken(model),
        provider: data.provider,
        deprecated: model.deprecated,
        periods: buildPeriods(model, baseTiers),
      };

      if (baseTiers) pricing.tiers = baseTiers;

      this.models.set(canonicalName, pricing);

      if (model.aliases) {
        for (const alias of model.aliases) {
          this.aliases.set(alias, canonicalName);
        }
      }
    }
  }

  /**
   * Normalize a model name by stripping common provider prefixes.
   */
  private normalize(model: string): string {
    return model
      .replace(/^anthropic\//, '')
      .replace(/^openai\//, '')
      .replace(/^google\//, '');
  }

  /**
   * Look up pricing for a model by canonical name or alias.
   */
  lookup(model: string): ModelPricing | null {
    return this.resolve(model)?.pricing ?? null;
  }

  /**
   * Resolve a model by canonical name or alias and return the canonical ID.
   */
  resolve(model: string): ResolvedModelPricing | null {
    const normalized = this.normalize(model.trim());
    if (!normalized) return null;

    // Try direct canonical match
    const direct = this.models.get(normalized);
    if (direct) {
      return { canonicalModel: normalized, pricing: direct };
    }

    // Try alias
    const canonical = this.aliases.get(normalized);
    if (canonical) {
      const pricing = this.models.get(canonical);
      if (pricing) return { canonicalModel: canonical, pricing };
    }

    return null;
  }

  /**
   * Calculate cost in USD for a set of token counts, priced at the rates in
   * force at `at` (a Date, epoch ms, or ISO string). Pass the event's own
   * timestamp so a dated rate change is applied by when the event happened;
   * omit it to price at the current period. Returns null if the model is not found.
   */
  calculate(model: string, tokens: TokenCounts, at?: PricingDate | null): number | null {
    const pricing = this.lookup(model);
    if (!pricing) return null;

    const rates = selectRates(pricing, tokens, resolveAtMs(at));
    return (tokens.input * rates.inputCostPerToken)
      + (tokens.output * rates.outputCostPerToken)
      + ((tokens.cacheRead ?? 0) * rates.cacheReadCostPerToken)
      + ((tokens.cacheWrite ?? 0) * rates.cacheWriteCostPerToken);
  }

  /**
   * Resolve the effective per-token rates for a request, selected by date (`at`)
   * then tier-selected by prompt size (uncached `input` + `cacheRead` +
   * `cacheWrite`). Flat models return their base rates. Returns null when the
   * model is unknown. Use this anywhere a cost/savings figure must agree with
   * `calculate()` on long-context tiered or dated pricing.
   */
  effectiveRates(model: string, tokens: TokenCounts, at?: PricingDate | null): PricingRates | null {
    const pricing = this.lookup(model);
    if (!pricing) return null;
    return selectRates(pricing, tokens, resolveAtMs(at));
  }

  /**
   * Check if a model is known to the registry.
   */
  has(model: string): boolean {
    return this.lookup(model) !== null;
  }

  /**
   * Get all known canonical model names.
   */
  get knownModels(): string[] {
    return [...this.models.keys()];
  }
}

// Singleton instance
export const pricingRegistry = new PricingRegistry();
