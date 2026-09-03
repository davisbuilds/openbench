import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { resolveDbPath } from './db-path.js';

type EnvMap = NodeJS.ProcessEnv;

function parseEnvInt(value: string | undefined, fallback: number, min: number = 0): number {
  if (!value) return fallback;
  const parsed = Number.parseInt(value, 10);
  if (Number.isNaN(parsed) || parsed < min) return fallback;
  return parsed;
}

function parseEnvBool(value: string | undefined, fallback: boolean): boolean {
  if (value == null || value === '') return fallback;
  const normalized = value.trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'no', 'off'].includes(normalized)) return false;
  return fallback;
}

function parseEnvList(value: string | undefined): string[] {
  if (!value) return [];
  const items = value
    .split(',')
    .map(item => item.trim())
    .filter(Boolean);

  return [...new Set(items)];
}

export type CodexLiveMode = 'otel-only' | 'exporter';
export type InsightsProvider = 'openai' | 'anthropic' | 'gemini';

interface QuotaConfig {
  codexPollIntervalMs: number;
}

interface LiveConfig {
  enabled: boolean;
  codexMode: CodexLiveMode;
  capture: {
    prompts: boolean;
    reasoning: boolean;
    toolArguments: boolean;
  };
  diffPayloadMaxBytes: number;
}

interface SyncConfig {
  excludePatterns: string[];
}

interface InsightProviderConfig {
  apiKey: string | null;
  model: string;
  baseUrl: string;
}

interface InsightsConfig {
  provider: InsightsProvider;
  providers: Record<InsightsProvider, InsightProviderConfig>;
}

interface UsageConfig {
  budgetsPath: string;
  findingsThresholdsPath: string;
}

interface WarehouseConfig {
  enabled: boolean;
  dsn: string | null;
  account: string;
  schema: string;
  biRole: string | null;
}

function isAgentMonitorRepo(dir: string): boolean {
  const packageJsonPath = path.join(dir, 'package.json');
  if (!fs.existsSync(packageJsonPath)) return false;

  try {
    const pkg = JSON.parse(fs.readFileSync(packageJsonPath, 'utf-8')) as { name?: string };
    return pkg.name === 'agentmonitor';
  } catch {
    return false;
  }
}

function detectProjectsDir(cwd: string): string {
  const start = path.resolve(cwd);
  let current = start;

  while (true) {
    if (isAgentMonitorRepo(current)) return path.dirname(current);
    const parent = path.dirname(current);
    if (parent === current) return start;
    current = parent;
  }
}

function resolveProjectsDir(env: EnvMap, cwd: string): string {
  const override = env.AGENTMONITOR_PROJECTS_DIR?.trim();
  if (override) return path.resolve(cwd, override);
  return detectProjectsDir(cwd);
}

function resolveClaudeDir(env: EnvMap, cwd: string): string {
  const override = env.AGENTMONITOR_CLAUDE_DIR?.trim();
  if (override) return path.resolve(cwd, expandTilde(override));
  return path.join(os.homedir(), '.claude');
}

function parseQuotaConfig(env: EnvMap): QuotaConfig {
  return {
    codexPollIntervalMs: parseEnvInt(env.AGENTMONITOR_CODEX_QUOTA_POLL_INTERVAL_MS, 60_000, 1_000),
  };
}

function parseCodexLiveMode(value: string | undefined): CodexLiveMode {
  return value === 'exporter' ? 'exporter' : 'otel-only';
}

function parseLiveConfig(env: EnvMap): LiveConfig {
  return {
    enabled: parseEnvBool(env.AGENTMONITOR_ENABLE_LIVE_TAB, true),
    codexMode: parseCodexLiveMode(env.AGENTMONITOR_CODEX_LIVE_MODE),
    capture: {
      prompts: parseEnvBool(env.AGENTMONITOR_LIVE_CAPTURE_PROMPTS, true),
      reasoning: parseEnvBool(env.AGENTMONITOR_LIVE_CAPTURE_REASONING, true),
      toolArguments: parseEnvBool(env.AGENTMONITOR_LIVE_CAPTURE_TOOL_ARGUMENTS, true),
    },
    diffPayloadMaxBytes: parseEnvInt(env.AGENTMONITOR_LIVE_DIFF_PAYLOAD_MAX_BYTES, 32768, 0),
  };
}

function parseInsightsConfig(env: EnvMap): InsightsConfig {
  const provider = ((): InsightsProvider => {
    const raw = env.AGENTMONITOR_INSIGHTS_PROVIDER?.trim().toLowerCase();
    return raw === 'anthropic' || raw === 'gemini' ? raw : 'openai';
  })();

  return {
    provider,
    providers: {
      openai: {
        apiKey: env.AGENTMONITOR_OPENAI_API_KEY?.trim() || env.OPENAI_API_KEY?.trim() || null,
        model: env.AGENTMONITOR_OPENAI_INSIGHTS_MODEL?.trim()
          || env.AGENTMONITOR_INSIGHTS_OPENAI_MODEL?.trim()
          || env.AGENTMONITOR_INSIGHTS_MODEL?.trim()
          || 'gpt-5-mini',
        baseUrl: (env.AGENTMONITOR_OPENAI_BASE_URL?.trim() || 'https://api.openai.com/v1').replace(/\/+$/, ''),
      },
      anthropic: {
        apiKey: env.AGENTMONITOR_ANTHROPIC_API_KEY?.trim() || env.ANTHROPIC_API_KEY?.trim() || null,
        model: env.AGENTMONITOR_ANTHROPIC_INSIGHTS_MODEL?.trim()
          || env.AGENTMONITOR_INSIGHTS_ANTHROPIC_MODEL?.trim()
          || 'claude-sonnet-4-5',
        baseUrl: (env.AGENTMONITOR_ANTHROPIC_BASE_URL?.trim() || 'https://api.anthropic.com/v1').replace(/\/+$/, ''),
      },
      gemini: {
        apiKey: env.AGENTMONITOR_GEMINI_API_KEY?.trim()
          || env.GEMINI_API_KEY?.trim()
          || env.GOOGLE_API_KEY?.trim()
          || null,
        model: env.AGENTMONITOR_GEMINI_INSIGHTS_MODEL?.trim()
          || env.AGENTMONITOR_INSIGHTS_GEMINI_MODEL?.trim()
          || 'gemini-2.5-flash',
        baseUrl: (env.AGENTMONITOR_GEMINI_BASE_URL?.trim() || 'https://generativelanguage.googleapis.com/v1beta').replace(/\/+$/, ''),
      },
    },
  };
}

function parseSyncConfig(env: EnvMap): SyncConfig {
  return {
    excludePatterns: parseEnvList(env.AGENTMONITOR_SYNC_EXCLUDE_PATTERNS),
  };
}

function expandTilde(dir: string): string {
  if (dir === '~') return os.homedir();
  if (dir.startsWith('~/')) return path.join(os.homedir(), dir.slice(2));
  return dir;
}

function parseSkillCatalogDirs(env: EnvMap): string[] {
  const override = env.AGENTMONITOR_SKILL_CATALOG_DIRS?.trim();
  if (override) {
    const dirs = override
      .split(path.delimiter)
      .map(item => item.trim())
      .filter(Boolean)
      .map(expandTilde);
    return [...new Set(dirs)];
  }

  const codexHome = env.CODEX_HOME?.trim() || path.join(os.homedir(), '.codex');
  return [
    path.join(os.homedir(), '.claude', 'skills'),
    path.join(codexHome, 'skills'),
  ];
}

function parseWarehouseConfig(env: EnvMap): WarehouseConfig {
  const dsn = env.AGENTMONITOR_WAREHOUSE_DSN?.trim() || null;
  const rawBiRole = env.AGENTMONITOR_WAREHOUSE_BI_ROLE?.trim();
  return {
    enabled: Boolean(dsn),
    dsn,
    account: env.AGENTMONITOR_WAREHOUSE_ACCOUNT?.trim() || 'local',
    schema: env.AGENTMONITOR_WAREHOUSE_SCHEMA?.trim() || 'agentmonitor',
    biRole: rawBiRole === '' ? null : (rawBiRole ?? 'medallion_bi'),
  };
}

export function createConfig(env: EnvMap = process.env, cwd: string = process.cwd()) {
  return {
    port: parseEnvInt(env.AGENTMONITOR_PORT, 3141, 1),
    host: env.AGENTMONITOR_HOST || '127.0.0.1',
    dbPath: resolveDbPath(env),
    maxPayloadKB: parseEnvInt(env.AGENTMONITOR_MAX_PAYLOAD_KB, 10, 0),
    sessionTimeoutMinutes: parseEnvInt(env.AGENTMONITOR_SESSION_TIMEOUT, 5, 1),
    maxFeed: parseEnvInt(env.AGENTMONITOR_MAX_FEED, 200, 1),
    statsIntervalMs: parseEnvInt(env.AGENTMONITOR_STATS_INTERVAL, 5000, 250),
    maxSseClients: parseEnvInt(env.AGENTMONITOR_MAX_SSE_CLIENTS, 50, 1),
    sseHeartbeatMs: parseEnvInt(env.AGENTMONITOR_SSE_HEARTBEAT_MS, 30000, 1000),
    autoImportIntervalMinutes: parseEnvInt(env.AGENTMONITOR_AUTO_IMPORT_MINUTES, 10, 0),
    projectsDir: resolveProjectsDir(env, cwd),
    claudeDir: resolveClaudeDir(env, cwd),
    quotas: parseQuotaConfig(env),
    contextWindow: {
      codexDefault: parseEnvInt(env.AGENTMONITOR_CODEX_CONTEXT_WINDOW, 256_000, 1),
    },
    live: parseLiveConfig(env),
    sync: parseSyncConfig(env),
    skillCatalogDirs: parseSkillCatalogDirs(env),
    usage: {
      budgetsPath: env.AGENTMONITOR_USAGE_BUDGETS_PATH?.trim() || './config/budgets.json',
      findingsThresholdsPath: env.AGENTMONITOR_TRACE_QUALITY_FINDINGS_PATH?.trim()
        || './config/trace-quality-findings.json',
    } satisfies UsageConfig,
    insights: parseInsightsConfig(env),
    warehouse: parseWarehouseConfig(env),
  };
}

export const config = createConfig();
