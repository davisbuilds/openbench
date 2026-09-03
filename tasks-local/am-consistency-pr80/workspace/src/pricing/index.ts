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

interface PricingDataModel extends PricingDataRates {
  aliases?: string[];
  deprecated: boolean;
  tiers?: PricingDataTier[];
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

export interface ModelPricing extends PricingRates {
  provider: string;
  deprecated: boolean;
  /** Higher prompt-size bands, ascending by threshold. Absent for flat models. */
  tiers?: PricingTier[];
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

// Pick the effective rates for a request. Flat models (no `tiers`) always use
// their base rates. Tiered models select by the request's prompt size — the
// input context, i.e. uncached input + cache reads + cache writes — applying the
// highest band whose threshold the prompt strictly exceeds (boundaries are exclusive).
function selectRates(pricing: ModelPricing, tokens: TokenCounts): PricingRates {
  if (!pricing.tiers || pricing.tiers.length === 0) return pricing;
  const promptTokens = tokens.input + (tokens.cacheRead ?? 0) + (tokens.cacheWrite ?? 0);
  let rates: PricingRates = pricing;
  for (const tier of pricing.tiers) {
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

    for (const file of ['claude.json', 'codex.json', 'gemini.json']) {
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
      const pricing: ModelPricing = {
        ...toPerToken(model),
        provider: data.provider,
        deprecated: model.deprecated,
      };

      if (model.tiers && model.tiers.length > 0) {
        pricing.tiers = model.tiers
          .map(tier => ({ ...toPerToken(tier), abovePromptTokens: tier.abovePromptTokens }))
          .sort((a, b) => a.abovePromptTokens - b.abovePromptTokens);
      }

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
   * Calculate cost in USD for a set of token counts.
   * Returns null if the model is not found.
   */
  calculate(model: string, tokens: TokenCounts): number | null {
    const pricing = this.lookup(model);
    if (!pricing) return null;

    const rates = selectRates(pricing, tokens);
    return (tokens.input * rates.inputCostPerToken)
      + (tokens.output * rates.outputCostPerToken)
      + ((tokens.cacheRead ?? 0) * rates.cacheReadCostPerToken)
      + ((tokens.cacheWrite ?? 0) * rates.cacheWriteCostPerToken);
  }

  /**
   * Resolve the effective per-token rates for a request, tier-selected by prompt
   * size (uncached `input` + `cacheRead` + `cacheWrite`). Flat models return their base rates.
   * Returns null when the model is unknown. Use this anywhere a cost/savings
   * figure must agree with `calculate()` on long-context tiered pricing.
   */
  effectiveRates(model: string, tokens: TokenCounts): PricingRates | null {
    const pricing = this.lookup(model);
    if (!pricing) return null;
    return selectRates(pricing, tokens);
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
