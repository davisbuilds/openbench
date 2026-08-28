import fs from 'node:fs';
import path from 'node:path';

import { config } from '../config.js';
import { getUsageSummary } from '../db/v2-queries.js';
import type {
  UsageBudgetAlertState,
  UsageBudgetFilters,
  UsageBudgetPeriod,
  UsageBudgetReport,
  UsageBudgetThresholds,
  UsageBudgetsResponse,
  UsageParams,
} from '../api/v2/types.js';

const DEFAULT_THRESHOLDS: UsageBudgetThresholds = {
  info: 50,
  warning: 75,
  critical: 90,
  hard_stop_candidate: 100,
};

interface BudgetConfigFile {
  budgets?: unknown;
}

interface ParsedBudgetConfig {
  name: string;
  period: UsageBudgetPeriod;
  limit_usd: number;
  thresholds: UsageBudgetThresholds;
  filters: UsageBudgetFilters;
}

function roundCurrency(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function roundPercent(value: number): number {
  return Math.round(value * 100) / 100;
}

function resolveBudgetPath(): string {
  return path.resolve(process.cwd(), config.usage.budgetsPath);
}

function normalizePeriod(value: unknown): UsageBudgetPeriod | null {
  if (typeof value !== 'string') return null;
  const normalized = value.trim().toLowerCase();
  if (normalized === 'daily') return 'day';
  if (normalized === 'weekly') return 'week';
  if (normalized === 'monthly') return 'month';
  if (normalized === 'day' || normalized === 'week' || normalized === 'month' || normalized === 'all_time') {
    return normalized;
  }
  return null;
}

function parseNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
  return value;
}

function parseThresholds(value: unknown, errors: string[], index: number): UsageBudgetThresholds {
  if (value == null) return DEFAULT_THRESHOLDS;
  if (!isRecord(value)) {
    errors.push(`budgets[${index}].thresholds must be an object when provided`);
    return DEFAULT_THRESHOLDS;
  }

  const thresholds = { ...DEFAULT_THRESHOLDS };
  for (const key of Object.keys(DEFAULT_THRESHOLDS) as Array<keyof UsageBudgetThresholds>) {
    if (value[key] == null) continue;
    const parsed = typeof value[key] === 'number' && Number.isFinite(value[key]) ? value[key] : null;
    if (parsed == null || parsed < 0) {
      errors.push(`budgets[${index}].thresholds.${key} must be a non-negative number`);
      continue;
    }
    thresholds[key] = parsed;
  }
  return thresholds;
}

function parseFilters(value: unknown, errors: string[], index: number): UsageBudgetFilters {
  if (value == null) return {};
  if (!isRecord(value)) {
    errors.push(`budgets[${index}].filters must be an object when provided`);
    return {};
  }

  const filters: UsageBudgetFilters = {};
  for (const key of ['project', 'agent', 'model', 'provider', 'tier'] as const) {
    if (value[key] == null) continue;
    if (typeof value[key] !== 'string' || value[key].trim() === '') {
      errors.push(`budgets[${index}].filters.${key} must be a non-empty string`);
      continue;
    }
    filters[key] = value[key].trim();
  }
  return filters;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseBudgetConfig(raw: unknown): { budgets: ParsedBudgetConfig[]; errors: string[] } {
  const errors: string[] = [];
  if (!isRecord(raw)) {
    return { budgets: [], errors: ['Budget config root must be an object'] };
  }

  const file = raw as BudgetConfigFile;
  if (file.budgets == null) {
    return { budgets: [], errors: [] };
  }
  if (!Array.isArray(file.budgets)) {
    return { budgets: [], errors: ['budgets must be an array'] };
  }

  const budgets: ParsedBudgetConfig[] = [];
  for (const [index, budget] of file.budgets.entries()) {
    if (!isRecord(budget)) {
      errors.push(`budgets[${index}] must be an object`);
      continue;
    }

    const name = typeof budget.name === 'string' && budget.name.trim() ? budget.name.trim() : null;
    const period = normalizePeriod(budget.period);
    const limitUsd = parseNumber(budget.limit_usd);
    const thresholds = parseThresholds(budget.thresholds, errors, index);
    const filters = parseFilters(budget.filters, errors, index);

    if (!name) errors.push(`budgets[${index}].name must be a non-empty string`);
    if (!period) errors.push(`budgets[${index}].period must be day, week, month, or all_time`);
    if (limitUsd == null) errors.push(`budgets[${index}].limit_usd must be a positive number`);

    if (!name || !period || limitUsd == null) continue;

    budgets.push({
      name,
      period,
      limit_usd: limitUsd,
      thresholds,
      filters,
    });
  }

  return { budgets, errors };
}

function localDateString(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function periodRange(period: UsageBudgetPeriod, now = new Date()): Pick<UsageParams, 'date_from' | 'date_to'> {
  if (period === 'all_time') return {};

  const from = new Date(now);
  if (period === 'day') {
    return { date_from: localDateString(from), date_to: localDateString(now) };
  }
  if (period === 'week') {
    from.setDate(from.getDate() - 6);
    return { date_from: localDateString(from), date_to: localDateString(now) };
  }

  from.setDate(1);
  return { date_from: localDateString(from), date_to: localDateString(now) };
}

function budgetState(percentUsed: number, thresholds: UsageBudgetThresholds): UsageBudgetAlertState {
  if (percentUsed >= thresholds.hard_stop_candidate) return 'hard_stop_candidate';
  if (percentUsed >= thresholds.critical) return 'critical';
  if (percentUsed >= thresholds.warning) return 'warning';
  if (percentUsed >= thresholds.info) return 'info';
  return 'ok';
}

function evaluateBudget(budget: ParsedBudgetConfig): UsageBudgetReport {
  const range = periodRange(budget.period);
  const summary = getUsageSummary({
    ...range,
    ...budget.filters,
  });
  const spentUsd = summary.total_cost_usd;
  const percentUsed = roundPercent((spentUsd / budget.limit_usd) * 100);

  return {
    name: budget.name,
    period: budget.period,
    limit_usd: budget.limit_usd,
    spent_usd: spentUsd,
    remaining_usd: roundCurrency(Math.max(0, budget.limit_usd - spentUsd)),
    percent_used: percentUsed,
    state: budgetState(percentUsed, budget.thresholds),
    thresholds: budget.thresholds,
    filters: budget.filters,
    date_from: range.date_from ?? null,
    date_to: range.date_to ?? null,
    enforcing: false,
  };
}

export function getUsageBudgets(): UsageBudgetsResponse {
  const configPath = resolveBudgetPath();
  if (!fs.existsSync(configPath)) {
    return {
      data: [],
      config: {
        path: configPath,
        present: false,
        valid: true,
        errors: [],
      },
    };
  }

  let raw: unknown;
  try {
    raw = JSON.parse(fs.readFileSync(configPath, 'utf-8')) as unknown;
  } catch (err) {
    return {
      data: [],
      config: {
        path: configPath,
        present: true,
        valid: false,
        errors: [`Failed to parse budget config JSON: ${err instanceof Error ? err.message : String(err)}`],
      },
    };
  }

  const parsed = parseBudgetConfig(raw);
  if (parsed.errors.length > 0) {
    return {
      data: [],
      config: {
        path: configPath,
        present: true,
        valid: false,
        errors: parsed.errors,
      },
    };
  }

  return {
    data: parsed.budgets.map(evaluateBudget),
    config: {
      path: configPath,
      present: true,
      valid: true,
      errors: [],
    },
  };
}
