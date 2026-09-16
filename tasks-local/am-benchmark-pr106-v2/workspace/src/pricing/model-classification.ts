import { pricingRegistry, type PricingRegistry } from './index.js';

export type ModelPricingStatus = 'known' | 'deprecated' | 'unknown';

export interface ModelClassification {
  raw_model: string;
  canonical_model: string;
  provider: string;
  family: string;
  tier: string;
  known: boolean;
  deprecated: boolean;
  pricing_status: ModelPricingStatus;
}

const DEPRECATED_MODELS = new Set([
  'claude-3-opus-20240229',
  'claude-3-sonnet-20240229',
  'claude-3-haiku-20240307',
]);

function stripProviderPrefix(model: string): string {
  return model
    .replace(/^anthropic\//, '')
    .replace(/^openai\//, '')
    .replace(/^google\//, '');
}

function inferProvider(model: string): string {
  if (model.startsWith('anthropic/') || model.startsWith('claude-')) return 'anthropic';
  if (model.startsWith('openai/') || model.startsWith('gpt-') || /^o\d/.test(model)) return 'openai';
  if (model.startsWith('google/') || model.startsWith('gemini-')) return 'google';
  return 'unknown';
}

function inferFamily(model: string, provider: string): string {
  if (model.includes('claude')) return 'claude';
  if (model.includes('gemini')) return 'gemini';
  if (model.includes('codex')) return 'codex';
  if (model.startsWith('gpt-')) return 'gpt';
  if (/^o\d/.test(model)) return 'reasoning';
  return provider === 'unknown' ? 'unknown' : provider;
}

function inferTier(model: string, provider: string): string {
  if (provider === 'anthropic') {
    if (model.includes('haiku')) return 'haiku';
    if (model.includes('sonnet')) return 'sonnet';
    if (model.includes('opus')) return 'opus';
    if (model.includes('fable')) return 'fable';
    return 'unknown';
  }

  if (provider === 'google') {
    if (model.includes('flash')) return 'flash';
    if (model.includes('pro')) return 'pro';
    if (model.includes('ultra')) return 'ultra';
    return 'unknown';
  }

  if (provider === 'openai') {
    if (/^o\d/.test(model) || model.includes('reasoning')) return 'reasoning';
    if (model === 'gpt-5.6-sol') return 'sol';
    if (model === 'gpt-5.6-terra') return 'terra';
    if (model === 'gpt-5.6-luna') return 'luna';
    if (model.includes('mini') || model.includes('nano')) return 'economy';
    if (model.includes('pro') || model.includes('max')) return 'premium';
    if (model.startsWith('gpt-')) return 'standard';
    return 'unknown';
  }

  return 'unknown';
}

export function classifyModel(
  model: string | null | undefined,
  registry: PricingRegistry = pricingRegistry,
): ModelClassification {
  const rawModel = model ?? 'unknown';
  const trimmed = rawModel.trim();
  if (!trimmed) {
    return {
      raw_model: rawModel,
      canonical_model: 'unknown',
      provider: 'unknown',
      family: 'unknown',
      tier: 'unknown',
      known: false,
      deprecated: false,
      pricing_status: 'unknown',
    };
  }

  const resolved = registry.resolve(trimmed);
  if (resolved) {
    const provider = resolved.pricing.provider;
    return {
      raw_model: rawModel,
      canonical_model: resolved.canonicalModel,
      provider,
      family: inferFamily(resolved.canonicalModel, provider),
      tier: inferTier(resolved.canonicalModel, provider),
      known: true,
      deprecated: resolved.pricing.deprecated,
      pricing_status: resolved.pricing.deprecated ? 'deprecated' : 'known',
    };
  }

  const canonical = stripProviderPrefix(trimmed);
  const provider = inferProvider(trimmed);
  const deprecated = DEPRECATED_MODELS.has(canonical);
  return {
    raw_model: rawModel,
    canonical_model: canonical || 'unknown',
    provider,
    family: inferFamily(canonical, provider),
    tier: inferTier(canonical, provider),
    known: false,
    deprecated,
    pricing_status: deprecated ? 'deprecated' : 'unknown',
  };
}

export function classifyModelForUsage(model: string): ModelClassification {
  return classifyModel(model);
}
