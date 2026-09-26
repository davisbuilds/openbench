import type { PublishLineage, WarehouseConfig, WarehouseRunRow } from './types.js';

const IDENTIFIER = /^[a-z_][a-z0-9_]*$/;

const RUN_COLUMNS = [
  'account',
  'session_id',
  'model',
  'input_tokens',
  'output_tokens',
  'cache_read_tokens',
  'cache_write_tokens',
  'cost_usd',
  'latency_ms',
  'observation_count',
  'error_count',
  'quality_score',
  'quality_grade',
  'project',
  'agent_type',
  'started_at',
  'day',
  'published_run_id',
] as const satisfies readonly (keyof WarehouseRunRow)[];

const LINEAGE_COLUMNS = [
  'run_id',
  'created_at',
  'account',
  'window_start',
  'window_end',
  'sessions_published',
  'sessions_suppressed',
  'min_batch',
  'amon_version',
  'grant_role',
  'grant_skipped',
] as const satisfies readonly (keyof PublishLineage)[];

export interface WarehousePublishResult {
  run_id: string;
  rows_published: number;
  grant_role: string | null;
  grant_skipped: boolean;
}

export interface WarehousePublishPlan extends WarehousePublishResult {
  dry_run: true;
  schema: string;
  statements: string[];
  rows: WarehouseRunRow[];
  lineage: PublishLineage;
}

export interface PlanRunsOptions {
  biRoleExists?: boolean;
}

function assertIdentifier(kind: string, value: string): void {
  if (!IDENTIFIER.test(value)) throw new Error(`invalid ${kind} identifier: ${value}`);
}

function schemaName(config: WarehouseConfig): string {
  assertIdentifier('schema', config.schema);
  if (config.biRole) assertIdentifier('role', config.biRole);
  return config.schema;
}

function ddlStatements(schema: string): string[] {
  return [
    `CREATE SCHEMA IF NOT EXISTS ${schema}`,
    `CREATE TABLE IF NOT EXISTS ${schema}.runs (` +
      'account text NOT NULL, ' +
      'session_id text NOT NULL, ' +
      'model text, ' +
      'input_tokens integer NOT NULL, ' +
      'output_tokens integer NOT NULL, ' +
      'cache_read_tokens integer NOT NULL, ' +
      'cache_write_tokens integer NOT NULL, ' +
      'cost_usd double precision NOT NULL, ' +
      'latency_ms integer NOT NULL, ' +
      'observation_count integer NOT NULL, ' +
      'error_count integer NOT NULL, ' +
      'quality_score double precision, ' +
      'quality_grade text, ' +
      'project text, ' +
      'agent_type text, ' +
      'started_at timestamptz NOT NULL, ' +
      'day date NOT NULL, ' +
      'published_run_id text NOT NULL, ' +
      'updated_at timestamptz NOT NULL DEFAULT now(), ' +
      'PRIMARY KEY (account, session_id))',
    `CREATE TABLE IF NOT EXISTS ${schema}.publish_run (` +
      'run_id text PRIMARY KEY, ' +
      'created_at timestamptz NOT NULL, ' +
      'account text NOT NULL, ' +
      'window_start timestamptz, ' +
      'window_end timestamptz, ' +
      'sessions_published integer NOT NULL, ' +
      'sessions_suppressed integer NOT NULL, ' +
      'min_batch integer NOT NULL, ' +
      'amon_version text NOT NULL, ' +
      'grant_role text, ' +
      'grant_skipped boolean NOT NULL)',
  ];
}

function runUpsertSql(schema: string): string {
  const columns = RUN_COLUMNS.join(', ');
  const placeholders = RUN_COLUMNS.map((_, index) => `$${index + 1}`).join(', ');
  const updates = RUN_COLUMNS
    .filter(column => column !== 'account' && column !== 'session_id')
    .map(column => `${column} = EXCLUDED.${column}`)
    .concat('updated_at = now()')
    .join(', ');
  return `INSERT INTO ${schema}.runs (${columns}) VALUES (${placeholders}) ` +
    `ON CONFLICT (account, session_id) DO UPDATE SET ${updates}`;
}

function lineageInsertSql(schema: string): string {
  const columns = LINEAGE_COLUMNS.join(', ');
  const placeholders = LINEAGE_COLUMNS.map((_, index) => `$${index + 1}`).join(', ');
  return `INSERT INTO ${schema}.publish_run (${columns}) VALUES (${placeholders})`;
}

function runValues(row: WarehouseRunRow): unknown[] {
  return RUN_COLUMNS.map(column => row[column]);
}

function lineageValues(lineage: PublishLineage): unknown[] {
  return LINEAGE_COLUMNS.map(column => lineage[column]);
}

function grantStatements(schema: string, role: string): string[] {
  return [
    `GRANT USAGE ON SCHEMA ${schema} TO ${role}`,
    `GRANT SELECT ON ALL TABLES IN SCHEMA ${schema} TO ${role}`,
  ];
}

function plannedGrantStatements(schema: string, config: WarehouseConfig, options: PlanRunsOptions): {
  statements: string[];
  grantSkipped: boolean;
} {
  if (!config.biRole) return { statements: [], grantSkipped: false };
  const lookup = 'SELECT 1 FROM pg_roles WHERE rolname = $1';
  if (options.biRoleExists === false) return { statements: [lookup], grantSkipped: true };
  return {
    statements: [lookup, ...grantStatements(schema, config.biRole)],
    grantSkipped: false,
  };
}

export function planRuns(
  rows: readonly WarehouseRunRow[],
  lineage: PublishLineage,
  config: WarehouseConfig,
  options: PlanRunsOptions = {},
): WarehousePublishPlan {
  const schema = schemaName(config);
  const grantPlan = plannedGrantStatements(schema, config, options);
  const plannedLineage = { ...lineage, grant_skipped: grantPlan.grantSkipped };
  return {
    dry_run: true,
    schema,
    run_id: plannedLineage.run_id,
    rows_published: rows.length,
    grant_role: config.biRole,
    grant_skipped: grantPlan.grantSkipped,
    rows: [...rows],
    lineage: plannedLineage,
    statements: [
      'BEGIN',
      ...ddlStatements(schema),
      runUpsertSql(schema),
      ...grantPlan.statements,
      lineageInsertSql(schema),
      'COMMIT',
    ],
  };
}

export async function publishRuns(
  rows: readonly WarehouseRunRow[],
  lineage: PublishLineage,
  config: WarehouseConfig,
): Promise<WarehousePublishResult> {
  if (!config.dsn) throw new Error('AGENTMONITOR_WAREHOUSE_DSN is required to publish warehouse rows');
  const schema = schemaName(config);
  const { Client } = await import('pg');
  const client = new Client({ connectionString: config.dsn });
  await client.connect();
  try {
    await client.query('BEGIN');
    for (const statement of ddlStatements(schema)) {
      await client.query(statement);
    }

    const upsert = runUpsertSql(schema);
    for (const row of rows) {
      await client.query(upsert, runValues(row));
    }

    let grantSkipped = false;
    if (config.biRole) {
      const role = await client.query('SELECT 1 FROM pg_roles WHERE rolname = $1', [config.biRole]);
      if (role.rowCount && role.rowCount > 0) {
        for (const statement of grantStatements(schema, config.biRole)) {
          await client.query(statement);
        }
      } else {
        grantSkipped = true;
      }
    }

    const finalLineage = { ...lineage, grant_skipped: grantSkipped };
    await client.query(lineageInsertSql(schema), lineageValues(finalLineage));
    await client.query('COMMIT');
    return {
      run_id: finalLineage.run_id,
      rows_published: rows.length,
      grant_role: config.biRole,
      grant_skipped: grantSkipped,
    };
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    await client.end();
  }
}
