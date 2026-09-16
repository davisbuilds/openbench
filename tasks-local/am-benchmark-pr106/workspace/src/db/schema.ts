import type { Database } from 'better-sqlite3';
import { getDb } from './connection.js';
import { pricingRegistry } from '../pricing/index.js';
import {
  TRACE_QUALITY_EXPORT_PROVIDERS,
  TRACE_QUALITY_EXPORT_STATUSES,
} from '../trace-quality/constants.js';

function sqlStringList(values: readonly string[]): string {
  return values.map(value => `'${value.replaceAll("'", "''")}'`).join(', ');
}

/**
 * Rebuild `trace_quality_export_state` without its legacy foreign keys to the
 * (now-dropped) `trace_quality_traces`/`_observations` tables. A DB created
 * before the reframe still carries those FKs; once the parent tables are dropped
 * (by the reclaim script) any insert into the kept seam would fail with
 * `no such table`. Self-healing and idempotent: a no-op once the table is FK-free
 * or absent. Both `initSchema` (startup) and the reclaim script call it, so the
 * seam is repaired before the parents disappear on either path.
 */
export function ensureTraceQualityExportStateFkFree(db: Database): void {
  const sql = (db.prepare(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='trace_quality_export_state'",
  ).get() as { sql: string } | undefined)?.sql;
  if (!sql || !/REFERENCES\s+trace_quality_(traces|observations)/i.test(sql)) return;

  const foreignKeys = Number(db.pragma('foreign_keys', { simple: true }));
  db.pragma('foreign_keys = OFF');
  try {
    db.exec('BEGIN');
    // Build the FK-free replacement under a temp name (so the old indexes keep
    // their names until the original is dropped), copy rows, swap, re-index.
    db.exec(`
      CREATE TABLE trace_quality_export_state_rebuilt (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL CHECK (provider IN (${sqlStringList(TRACE_QUALITY_EXPORT_PROVIDERS)})),
        local_trace_id TEXT NOT NULL,
        local_observation_id TEXT,
        external_trace_id TEXT,
        external_observation_id TEXT,
        payload_hash TEXT,
        status TEXT NOT NULL CHECK (status IN (${sqlStringList(TRACE_QUALITY_EXPORT_STATUSES)})),
        exported_at TEXT,
        error_message TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );
      INSERT INTO trace_quality_export_state_rebuilt (
        id, provider, local_trace_id, local_observation_id, external_trace_id,
        external_observation_id, payload_hash, status, exported_at, error_message,
        metadata_json, created_at
      )
      SELECT id, provider, local_trace_id, local_observation_id, external_trace_id,
        external_observation_id, payload_hash, status, exported_at, error_message,
        metadata_json, created_at
      FROM trace_quality_export_state;
      DROP TABLE trace_quality_export_state;
      ALTER TABLE trace_quality_export_state_rebuilt RENAME TO trace_quality_export_state;
      CREATE INDEX IF NOT EXISTS idx_tq_export_provider_status ON trace_quality_export_state(provider, status, exported_at DESC);
      CREATE INDEX IF NOT EXISTS idx_tq_export_local_trace ON trace_quality_export_state(local_trace_id);
      CREATE INDEX IF NOT EXISTS idx_tq_export_local_observation ON trace_quality_export_state(local_observation_id);
      CREATE INDEX IF NOT EXISTS idx_tq_export_external_trace ON trace_quality_export_state(provider, external_trace_id);
    `);
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  } finally {
    db.pragma(`foreign_keys = ${foreignKeys ? 'ON' : 'OFF'}`);
  }
}

export function initSchema(): void {
  const db = getDb();

  db.exec(`
    CREATE TABLE IF NOT EXISTS agents (
      id TEXT PRIMARY KEY,
      agent_type TEXT NOT NULL,
      name TEXT,
      registered_at TEXT NOT NULL DEFAULT (datetime('now')),
      last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL,
      agent_type TEXT NOT NULL,
      project TEXT,
      branch TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      started_at TEXT NOT NULL DEFAULT (datetime('now')),
      ended_at TEXT,
      last_event_at TEXT NOT NULL DEFAULT (datetime('now')),
      metadata TEXT DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_id TEXT UNIQUE,
      schema_version INTEGER NOT NULL DEFAULT 1,
      session_id TEXT NOT NULL,
      agent_type TEXT NOT NULL,
      event_type TEXT NOT NULL,
      tool_name TEXT,
      status TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error', 'timeout')),
      tokens_in INTEGER DEFAULT 0,
      tokens_out INTEGER DEFAULT 0,
      branch TEXT,
      project TEXT,
      duration_ms INTEGER,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      client_timestamp TEXT,
      metadata TEXT DEFAULT '{}',
      payload_truncated INTEGER NOT NULL DEFAULT 0 CHECK (payload_truncated IN (0, 1)),
      model TEXT,
      cost_usd REAL,
      cache_read_tokens INTEGER DEFAULT 0,
      cache_write_tokens INTEGER DEFAULT 0,
      source TEXT DEFAULT 'api',
      study_id TEXT,
      study TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
    CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
    CREATE INDEX IF NOT EXISTS idx_events_tool_name ON events(tool_name);
    CREATE INDEX IF NOT EXISTS idx_events_agent_type ON events(agent_type);
    CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
  `);
  // NOTE: the bare session_id index and model coverage are handled in the
  // index-hygiene block after the column-presence migrations below, because the
  // covering composites reference cost_usd (added by an ALTER guard). The
  // agent_type/event_type indexes above are intentionally kept — see that block.

  // Import state tracking - avoids re-importing unchanged files
  db.exec(`
    CREATE TABLE IF NOT EXISTS import_state (
      file_path TEXT PRIMARY KEY,
      file_hash TEXT NOT NULL,
      file_size INTEGER NOT NULL,
      source TEXT NOT NULL,
      events_imported INTEGER NOT NULL DEFAULT 0,
      imported_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
  `);

  db.exec(`
    CREATE TABLE IF NOT EXISTS provider_quotas (
      provider TEXT PRIMARY KEY,
      agent_type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'unavailable',
      source TEXT,
      updated_at TEXT,
      account_label TEXT,
      plan_type TEXT,
      limit_id TEXT,
      limit_name TEXT,
      error_message TEXT,
      primary_used_percent REAL,
      primary_window_minutes INTEGER,
      primary_resets_at TEXT,
      secondary_used_percent REAL,
      secondary_window_minutes INTEGER,
      secondary_resets_at TEXT,
      credits_has_credits INTEGER,
      credits_unlimited INTEGER,
      credits_balance TEXT,
      raw_payload TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_provider_quotas_updated_at ON provider_quotas(updated_at DESC);
  `);

  // Backward-compatible schema updates for existing local databases.
  const eventColumns = new Set<string>(
    (db.prepare(`PRAGMA table_info(events)`).all() as Array<{ name: string }>).map(col => col.name)
  );

  if (!eventColumns.has('client_timestamp')) {
    db.exec('ALTER TABLE events ADD COLUMN client_timestamp TEXT');
  }
  if (!eventColumns.has('payload_truncated')) {
    db.exec('ALTER TABLE events ADD COLUMN payload_truncated INTEGER NOT NULL DEFAULT 0');
  }
  if (!eventColumns.has('model')) {
    db.exec('ALTER TABLE events ADD COLUMN model TEXT');
  }
  if (!eventColumns.has('cost_usd')) {
    db.exec('ALTER TABLE events ADD COLUMN cost_usd REAL');
  }
  if (!eventColumns.has('cache_read_tokens')) {
    db.exec('ALTER TABLE events ADD COLUMN cache_read_tokens INTEGER DEFAULT 0');
  }
  if (!eventColumns.has('cache_write_tokens')) {
    db.exec('ALTER TABLE events ADD COLUMN cache_write_tokens INTEGER DEFAULT 0');
  }
  if (!eventColumns.has('source')) {
    db.exec("ALTER TABLE events ADD COLUMN source TEXT DEFAULT 'api'");
  }
  // Benchmark study grouping (source='benchmark' rows only; null elsewhere).
  // study_id = openbench study_sha256 (exact per-run key, the grouping key);
  // study = the human slug label. See docs/specs/2026-09-02-benchmark-comparison-view-spec.md.
  if (!eventColumns.has('study_id')) {
    db.exec('ALTER TABLE events ADD COLUMN study_id TEXT');
  }
  if (!eventColumns.has('study')) {
    db.exec('ALTER TABLE events ADD COLUMN study TEXT');
  }
  db.exec('CREATE INDEX IF NOT EXISTS idx_events_study_id ON events(study_id) WHERE study_id IS NOT NULL');

  const providerQuotaColumns = new Set<string>(
    (db.prepare(`PRAGMA table_info(provider_quotas)`).all() as Array<{ name: string }>).map(col => col.name)
  );

  if (!providerQuotaColumns.has('provider') || !providerQuotaColumns.has('agent_type')) {
    db.exec('DROP TABLE IF EXISTS provider_quotas');
    db.exec(`
      CREATE TABLE IF NOT EXISTS provider_quotas (
        provider TEXT PRIMARY KEY,
        agent_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'unavailable',
        source TEXT,
        updated_at TEXT,
        account_label TEXT,
        plan_type TEXT,
        limit_id TEXT,
        limit_name TEXT,
        error_message TEXT,
        primary_used_percent REAL,
        primary_window_minutes INTEGER,
        primary_resets_at TEXT,
        secondary_used_percent REAL,
        secondary_window_minutes INTEGER,
        secondary_resets_at TEXT,
        credits_has_credits INTEGER,
        credits_unlimited INTEGER,
        credits_balance TEXT,
        raw_payload TEXT
      );

      CREATE INDEX IF NOT EXISTS idx_provider_quotas_updated_at ON provider_quotas(updated_at DESC);
    `);
  } else {
    if (!providerQuotaColumns.has('status')) {
      db.exec("ALTER TABLE provider_quotas ADD COLUMN status TEXT NOT NULL DEFAULT 'unavailable'");
    }
    if (!providerQuotaColumns.has('source')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN source TEXT');
    }
    if (!providerQuotaColumns.has('updated_at')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN updated_at TEXT');
    }
    if (!providerQuotaColumns.has('account_label')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN account_label TEXT');
    }
    if (!providerQuotaColumns.has('plan_type')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN plan_type TEXT');
    }
    if (!providerQuotaColumns.has('limit_id')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN limit_id TEXT');
    }
    if (!providerQuotaColumns.has('limit_name')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN limit_name TEXT');
    }
    if (!providerQuotaColumns.has('error_message')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN error_message TEXT');
    }
    if (!providerQuotaColumns.has('primary_used_percent')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN primary_used_percent REAL');
    }
    if (!providerQuotaColumns.has('primary_window_minutes')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN primary_window_minutes INTEGER');
    }
    if (!providerQuotaColumns.has('primary_resets_at')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN primary_resets_at TEXT');
    }
    if (!providerQuotaColumns.has('secondary_used_percent')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN secondary_used_percent REAL');
    }
    if (!providerQuotaColumns.has('secondary_window_minutes')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN secondary_window_minutes INTEGER');
    }
    if (!providerQuotaColumns.has('secondary_resets_at')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN secondary_resets_at TEXT');
    }
    if (!providerQuotaColumns.has('credits_has_credits')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN credits_has_credits INTEGER');
    }
    if (!providerQuotaColumns.has('credits_unlimited')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN credits_unlimited INTEGER');
    }
    if (!providerQuotaColumns.has('credits_balance')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN credits_balance TEXT');
    }
    if (!providerQuotaColumns.has('raw_payload')) {
      db.exec('ALTER TABLE provider_quotas ADD COLUMN raw_payload TEXT');
    }
  }

  db.exec('UPDATE events SET payload_truncated = 0 WHERE payload_truncated IS NULL');
  db.exec(`
    UPDATE events
    SET metadata = json_quote(CAST(metadata AS TEXT))
    WHERE metadata IS NOT NULL AND json_valid(metadata) = 0
  `);

  // Remove restrictive CHECK constraint on event_type if present on existing databases.
  // SQLite does not support ALTER CONSTRAINT, so we recreate the table.
  const tableSql = (db.prepare(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
  ).get() as { sql: string } | undefined)?.sql ?? '';

  if (tableSql.includes('CHECK (event_type IN')) {
    db.exec(`
      CREATE TABLE events_migrated (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT UNIQUE,
        schema_version INTEGER NOT NULL DEFAULT 1,
        session_id TEXT NOT NULL,
        agent_type TEXT NOT NULL,
        event_type TEXT NOT NULL,
        tool_name TEXT,
        status TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error', 'timeout')),
        tokens_in INTEGER DEFAULT 0,
        tokens_out INTEGER DEFAULT 0,
        branch TEXT,
        project TEXT,
        duration_ms INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        client_timestamp TEXT,
        metadata TEXT DEFAULT '{}',
        payload_truncated INTEGER NOT NULL DEFAULT 0 CHECK (payload_truncated IN (0, 1)),
        model TEXT,
        cost_usd REAL,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_write_tokens INTEGER DEFAULT 0,
        source TEXT DEFAULT 'api'
      );

      INSERT INTO events_migrated (
        id, event_id, schema_version, session_id, agent_type, event_type, tool_name,
        status, tokens_in, tokens_out, branch, project, duration_ms,
        created_at, client_timestamp, metadata, payload_truncated,
        model, cost_usd, cache_read_tokens, cache_write_tokens, source
      )
      SELECT
        id, event_id, schema_version, session_id, agent_type, event_type, tool_name,
        status, tokens_in, tokens_out, branch, project, duration_ms,
        created_at, client_timestamp, metadata, payload_truncated,
        model, cost_usd,
        COALESCE(cache_read_tokens, 0), COALESCE(cache_write_tokens, 0),
        COALESCE(source, 'api')
      FROM events;

      DROP TABLE events;
      ALTER TABLE events_migrated RENAME TO events;

      CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
      CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
      CREATE INDEX IF NOT EXISTS idx_events_tool_name ON events(tool_name);
      CREATE INDEX IF NOT EXISTS idx_events_agent_type ON events(agent_type);
      CREATE INDEX IF NOT EXISTS idx_events_model ON events(model);
    `);
  }

  // Event index hygiene (schema-storage-rebalance Phase 1).
  // - Replace the bare session_id index with a covering composite so the monitor
  //   session-list SUM(tokens_in/out, cost_usd) subqueries resolve index-only.
  //   The composite's leftmost column is session_id, so plain session_id
  //   equality lookups (and DISTINCT session_id) still use it.
  // - Add a covering (created_at, model, ...) composite for the time-windowed
  //   cost/usage aggregates.
  // Created here (not with the base table) because the covering columns include
  // cost_usd, which is added by an ALTER guard above on legacy databases.
  //
  // NOTE: idx_events_agent_type / idx_events_event_type are deliberately NOT
  // dropped. They are too low-cardinality to help row *filtering*, but they are
  // the covering indexes for the filter-option `SELECT DISTINCT agent_type/
  // event_type ... ORDER BY` enumeration (src/db/queries.ts, v2-queries.ts).
  // Without them that dashboard-bootstrap read regresses from a covering-index
  // scan to a full events scan + temp b-tree.
  //
  // - idx_events_session_reconcile seeds the correlated Codex OTEL/import
  //   usage-reconciliation subquery (src/db/usage-reconciliation.ts). That
  //   subquery correlates on session_id (highly selective) then filters
  //   agent_type='codex' AND source='import'. Without this index the planner
  //   falls back to seeking idx_events_agent_type (agent_type=?), matching every
  //   Codex row, turning the full-history stats aggregate into an O(n^2) scan
  //   (measured ~95s per run on ~440k events; ~0.2s with this index).
  //
  // - idx_events_usage_ts is an EXPRESSION index, and the expression must stay
  //   character-identical to usageTimestampExpr()/the date predicate in
  //   v2-queries.ts or SQLite silently ignores it and reverts to a full scan.
  //   Every usage read filters on datetime(COALESCE(client_timestamp,
  //   created_at)); wrapping the column in datetime() is not sloppiness but
  //   load-bearing normalization, because the two columns store different
  //   formats (client_timestamp '2026-02-19T02:07:56.664Z' vs created_at
  //   '2026-02-20 04:21:39'). That means the predicate cannot be made sargable
  //   by comparing raw strings — 'T' (0x54) and ' ' (0x20) sort differently, so
  //   a string compare would silently mis-bucket same-day rows. Indexing the
  //   normalized expression keeps the semantics and gets the seek:
  //   SCAN (497k rows) -> SEARCH USING INDEX.
  db.exec(`
    DROP INDEX IF EXISTS idx_events_session_id;
    CREATE INDEX IF NOT EXISTS idx_events_model ON events(model);
    CREATE INDEX IF NOT EXISTS idx_events_session_cost
      ON events(session_id, tokens_in, tokens_out, cost_usd);
    CREATE INDEX IF NOT EXISTS idx_events_created_model
      ON events(created_at, model, tokens_in, tokens_out, cost_usd);
    CREATE INDEX IF NOT EXISTS idx_events_session_reconcile
      ON events(session_id, agent_type, source);
    CREATE INDEX IF NOT EXISTS idx_events_usage_ts
      ON events(datetime(COALESCE(client_timestamp, created_at)));
  `);

  // --- V2 tables: session browser, messages, tool calls, FTS ---

  db.exec(`
    CREATE TABLE IF NOT EXISTS browsing_sessions (
      id TEXT PRIMARY KEY,
      project TEXT,
      agent TEXT NOT NULL,
      first_message TEXT,
      started_at TEXT,
      ended_at TEXT,
      message_count INTEGER NOT NULL DEFAULT 0,
      user_message_count INTEGER NOT NULL DEFAULT 0,
      parent_session_id TEXT,
      relationship_type TEXT,
      live_status TEXT,
      last_item_at TEXT,
      integration_mode TEXT,
      fidelity TEXT,
      capabilities_json TEXT,
      file_path TEXT,
      file_size INTEGER,
      file_hash TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_bs_ended_at ON browsing_sessions(ended_at DESC);
    CREATE INDEX IF NOT EXISTS idx_bs_project ON browsing_sessions(project);
    CREATE INDEX IF NOT EXISTS idx_bs_agent ON browsing_sessions(agent);
    CREATE INDEX IF NOT EXISTS idx_bs_started_at ON browsing_sessions(started_at);
  `);

  const browsingSessionColumns = new Set<string>(
    (db.prepare(`PRAGMA table_info(browsing_sessions)`).all() as Array<{ name: string }>).map(col => col.name)
  );

  if (!browsingSessionColumns.has('live_status')) {
    db.exec('ALTER TABLE browsing_sessions ADD COLUMN live_status TEXT');
  }
  if (!browsingSessionColumns.has('last_item_at')) {
    db.exec('ALTER TABLE browsing_sessions ADD COLUMN last_item_at TEXT');
  }
  if (!browsingSessionColumns.has('integration_mode')) {
    db.exec('ALTER TABLE browsing_sessions ADD COLUMN integration_mode TEXT');
  }
  if (!browsingSessionColumns.has('fidelity')) {
    db.exec('ALTER TABLE browsing_sessions ADD COLUMN fidelity TEXT');
  }
  if (!browsingSessionColumns.has('capabilities_json')) {
    db.exec('ALTER TABLE browsing_sessions ADD COLUMN capabilities_json TEXT');
  }
  if (!browsingSessionColumns.has('context_used_tokens')) {
    db.exec('ALTER TABLE browsing_sessions ADD COLUMN context_used_tokens INTEGER');
  }
  if (!browsingSessionColumns.has('context_window_tokens')) {
    db.exec('ALTER TABLE browsing_sessions ADD COLUMN context_window_tokens INTEGER');
  }
  if (!browsingSessionColumns.has('project_identity')) {
    db.exec('ALTER TABLE browsing_sessions ADD COLUMN project_identity TEXT');
  }
  if (!browsingSessionColumns.has('skill_context_capabilities_json')) {
    db.exec(`
      ALTER TABLE browsing_sessions
      ADD COLUMN skill_context_capabilities_json TEXT
        CHECK (skill_context_capabilities_json IS NULL OR json_valid(skill_context_capabilities_json))
    `);
  }

  db.exec('CREATE INDEX IF NOT EXISTS idx_bs_last_item_at ON browsing_sessions(last_item_at DESC)');
  db.exec('CREATE INDEX IF NOT EXISTS idx_bs_live_status ON browsing_sessions(live_status)');

  db.exec(`
    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      role TEXT NOT NULL,
      content TEXT NOT NULL,
      timestamp TEXT,
      has_thinking INTEGER NOT NULL DEFAULT 0,
      has_tool_use INTEGER NOT NULL DEFAULT 0,
      content_length INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_messages_session_ordinal ON messages(session_id, ordinal);
    CREATE INDEX IF NOT EXISTS idx_messages_session_role ON messages(session_id, role);
  `);

  db.exec(`
    CREATE TABLE IF NOT EXISTS pinned_messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      message_id INTEGER,
      message_ordinal INTEGER NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE(session_id, message_ordinal)
    );

    CREATE INDEX IF NOT EXISTS idx_pm_session_ordinal ON pinned_messages(session_id, message_ordinal);
    CREATE INDEX IF NOT EXISTS idx_pm_created_at ON pinned_messages(created_at DESC);
  `);

  db.exec(`
    CREATE TABLE IF NOT EXISTS insights (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL,
      title TEXT NOT NULL,
      prompt TEXT,
      content TEXT NOT NULL,
      date_from TEXT NOT NULL,
      date_to TEXT NOT NULL,
      project TEXT,
      agent TEXT,
      provider TEXT NOT NULL,
      model TEXT NOT NULL,
      analytics_summary_json TEXT NOT NULL,
      analytics_coverage_json TEXT NOT NULL,
      usage_summary_json TEXT NOT NULL,
      usage_coverage_json TEXT NOT NULL,
      input_json TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_insights_created_at ON insights(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_insights_scope ON insights(kind, date_from, date_to, project, agent);
  `);

  db.exec(`
    CREATE TABLE IF NOT EXISTS tool_calls (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      message_id INTEGER NOT NULL,
      session_id TEXT NOT NULL,
      tool_name TEXT NOT NULL,
      category TEXT,
      tool_use_id TEXT,
      input_json TEXT,
      result_content TEXT,
      result_content_length INTEGER,
      subagent_session_id TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_tc_session_id ON tool_calls(session_id);
    CREATE INDEX IF NOT EXISTS idx_tc_category ON tool_calls(category);
    CREATE INDEX IF NOT EXISTS idx_tc_tool_name ON tool_calls(tool_name);
  `);

  db.exec(`
    CREATE TABLE IF NOT EXISTS session_context_observations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      kind TEXT NOT NULL CHECK (
        kind IN ('consultation', 'compaction', 'catalog_presentation', 'instruction_load')
      ),
      source TEXT NOT NULL,
      observed_at TEXT,
      skill_name TEXT,
      command_fingerprint TEXT,
      project_identity TEXT,
      reason TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json))
    );

    CREATE INDEX IF NOT EXISTS idx_sco_session_kind_name_ordinal
      ON session_context_observations(session_id, kind, skill_name, ordinal);
    CREATE INDEX IF NOT EXISTS idx_sco_skill_time
      ON session_context_observations(skill_name, observed_at);

    CREATE TABLE IF NOT EXISTS session_catalog_observation_entries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      observation_id INTEGER NOT NULL,
      ordinal INTEGER NOT NULL,
      skill_name TEXT NOT NULL,
      description TEXT,
      description_fingerprint TEXT,
      source_location TEXT,
      scope TEXT,
      FOREIGN KEY(observation_id) REFERENCES session_context_observations(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_scoe_observation_ordinal
      ON session_catalog_observation_entries(observation_id, ordinal);
    CREATE INDEX IF NOT EXISTS idx_scoe_skill
      ON session_catalog_observation_entries(skill_name);
  `);

  // Immutable desired-state evidence is stored separately from parser-owned
  // session projections. The session association intentionally has no foreign
  // key to browsing_sessions: transcript reparse deletes and recreates that row
  // inside one transaction, while the explicit realization binding must survive.
  db.exec(`
    CREATE TABLE IF NOT EXISTS skill_expected_realizations (
      id TEXT PRIMARY KEY,
      harness TEXT NOT NULL CHECK (harness IN ('claude', 'codex')),
      profile_identity TEXT NOT NULL,
      canonical_revision TEXT NOT NULL,
      valid_from TEXT NOT NULL,
      valid_to TEXT,
      payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
      content_hash TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_ser_harness_validity
      ON skill_expected_realizations(harness, valid_from, valid_to);

    CREATE TABLE IF NOT EXISTS session_expected_skill_realizations (
      session_id TEXT PRIMARY KEY,
      realization_id TEXT NOT NULL,
      associated_at TEXT NOT NULL DEFAULT (datetime('now')),
      FOREIGN KEY(realization_id) REFERENCES skill_expected_realizations(id)
    );

    CREATE INDEX IF NOT EXISTS idx_sesr_realization
      ON session_expected_skill_realizations(realization_id);
  `);

  db.exec(`
    CREATE TABLE IF NOT EXISTS session_turns (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      agent_type TEXT NOT NULL,
      source_turn_id TEXT,
      status TEXT,
      title TEXT,
      started_at TEXT,
      ended_at TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_st_session_started_at ON session_turns(session_id, started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_st_source_turn_id ON session_turns(source_turn_id);
  `);

  db.exec(`
    CREATE TABLE IF NOT EXISTS session_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      turn_id INTEGER,
      ordinal INTEGER NOT NULL DEFAULT 0,
      source_item_id TEXT,
      kind TEXT NOT NULL,
      status TEXT,
      payload_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT,
      FOREIGN KEY(turn_id) REFERENCES session_turns(id) ON DELETE SET NULL
    );

    CREATE INDEX IF NOT EXISTS idx_si_session_created_at ON session_items(session_id, created_at, id);
    CREATE INDEX IF NOT EXISTS idx_si_turn_ordinal ON session_items(turn_id, ordinal);
    CREATE INDEX IF NOT EXISTS idx_si_source_item_id ON session_items(source_item_id);
  `);

  // Trace-quality reframe (Phase 3): the persisted trace/observation/score/prompt
  // warehouse is gone — detail is projected on-demand and only the lean
  // session_trace_summary is stored. The dormant export seam is kept (no FK to
  // the dropped tables); existing DBs are reclaimed by scripts/reclaim-trace-quality.ts.
  db.exec(`
    CREATE TABLE IF NOT EXISTS trace_quality_export_state (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      provider TEXT NOT NULL CHECK (provider IN (${sqlStringList(TRACE_QUALITY_EXPORT_PROVIDERS)})),
      local_trace_id TEXT NOT NULL,
      local_observation_id TEXT,
      external_trace_id TEXT,
      external_observation_id TEXT,
      payload_hash TEXT,
      status TEXT NOT NULL CHECK (status IN (${sqlStringList(TRACE_QUALITY_EXPORT_STATUSES)})),
      exported_at TEXT,
      error_message TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_tq_export_provider_status ON trace_quality_export_state(provider, status, exported_at DESC);
    CREATE INDEX IF NOT EXISTS idx_tq_export_local_trace ON trace_quality_export_state(local_trace_id);
    CREATE INDEX IF NOT EXISTS idx_tq_export_local_observation ON trace_quality_export_state(local_observation_id);
    CREATE INDEX IF NOT EXISTS idx_tq_export_external_trace ON trace_quality_export_state(provider, external_trace_id);
  `);

  // Repair the export seam on existing DBs: drop its legacy FKs to the removed
  // warehouse tables so it stays usable after the reclaim drops those parents.
  ensureTraceQualityExportStateFkFree(db);

  // Lean, content-free, export-shaped per-session trace summary (trace-quality
  // reframe). One row per session; the full observation tree is projected
  // on-demand rather than persisted. Columns map to medallion's
  // `silver.agent_runs` so the deferred export is near-free. No message text.
  db.exec(`
    CREATE TABLE IF NOT EXISTS session_trace_summary (
      session_id         TEXT PRIMARY KEY,
      trace_id           TEXT,
      agent_type         TEXT,
      project            TEXT,
      primary_model      TEXT,
      started_at         TEXT,
      ended_at           TEXT,
      observation_count  INTEGER NOT NULL DEFAULT 0,
      error_count        INTEGER NOT NULL DEFAULT 0,
      tokens_in          INTEGER NOT NULL DEFAULT 0,
      tokens_out         INTEGER NOT NULL DEFAULT 0,
      cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
      cache_write_tokens INTEGER NOT NULL DEFAULT 0,
      cost_usd           REAL NOT NULL DEFAULT 0,
      latency_ms_total   INTEGER NOT NULL DEFAULT 0,
      coverage_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(coverage_json)),
      quality_score      REAL,
      quality_grade      TEXT,
      projection_version TEXT NOT NULL,
      updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_sts_started ON session_trace_summary(started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_sts_quality ON session_trace_summary(quality_score);
  `);

  // Backward-compatible add of the stable per-session trace id (reframe Phase 2):
  // the on-demand read layer emits it as each row's id and reverses it to a
  // session for detail. Populated by the summary maintainers; the version bump
  // (sts:v3) re-backfills existing rows so the column fills in on upgrade.
  const sessionTraceSummaryColumns = new Set<string>(
    (db.prepare(`PRAGMA table_info(session_trace_summary)`).all() as Array<{ name: string }>).map(col => col.name)
  );
  if (!sessionTraceSummaryColumns.has('trace_id')) {
    db.exec('ALTER TABLE session_trace_summary ADD COLUMN trace_id TEXT');
  }
  // project: carried on the summary so the lean list honors the project filter
  // without re-projecting (filter parity with the old persisted trace list).
  if (!sessionTraceSummaryColumns.has('project')) {
    db.exec('ALTER TABLE session_trace_summary ADD COLUMN project TEXT');
  }
  db.exec('CREATE UNIQUE INDEX IF NOT EXISTS idx_sts_trace ON session_trace_summary(trace_id)');
  db.exec('CREATE INDEX IF NOT EXISTS idx_sts_project ON session_trace_summary(project)');

  // FTS5 full-text search on message content
  db.exec(`
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
      content,
      content=messages,
      content_rowid=id,
      tokenize='porter unicode61'
    );
  `);

  // Triggers to keep FTS index in sync with messages table
  const ftsTriggersExist = db.prepare(
    "SELECT name FROM sqlite_master WHERE type='trigger' AND name='messages_fts_insert'"
  ).get();

  if (!ftsTriggersExist) {
    db.exec(`
      CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
      END;

      CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
      END;

      CREATE TRIGGER messages_fts_update AFTER UPDATE OF content ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
      END;
    `);
  }

  // File-watcher state tracking
  db.exec(`
    CREATE TABLE IF NOT EXISTS watched_files (
      file_path TEXT PRIMARY KEY,
      file_hash TEXT NOT NULL,
      file_mtime TEXT,
      status TEXT NOT NULL DEFAULT 'parsed',
      last_parsed_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
  `);

  // Point-in-time record of which skill versions were installed when. A refresh
  // stamps each observed (name, version) pair; the [first_seen_at, last_seen_at]
  // window lets a past skill invocation be attributed to the version installed
  // at that time. Uniqueness for version-less rows is enforced in application
  // code (see refreshCatalogSnapshots), since a nullable version can't
  // participate in a NULL-distinct primary key.
  db.exec(`
    CREATE TABLE IF NOT EXISTS skill_catalog_snapshots (
      name TEXT NOT NULL,
      version TEXT,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      PRIMARY KEY (name, version)
    );

    CREATE INDEX IF NOT EXISTS idx_scs_name ON skill_catalog_snapshots(name);
  `);

  runDataMigrations(db);
}

// Schema-version counter for one-shot data corrections (distinct from the
// column-presence guards above, which handle additive DDL idempotently).
const DATA_SCHEMA_VERSION = 4;

/**
 * Apply one-shot, idempotent data corrections guarded by PRAGMA user_version.
 * Each runs at most once and is wrapped in a transaction so a crash mid-run
 * cannot leave a partially-migrated table (which would risk double-correction).
 */
export function runDataMigrations(db: Database): void {
  const current = (db.pragma('user_version', { simple: true }) as number) ?? 0;
  if (current >= DATA_SCHEMA_VERSION) return;

  // Apply the data changes and advance the version counter in one transaction.
  // PRAGMA user_version is itself transactional, so a crash mid-migration rolls
  // back both — there is no window where rows are corrected but the version is
  // not yet bumped (which would re-run and double-subtract on restart).
  const run = db.transaction(() => {
    if (current < 1) backfillCacheInclusiveInputTokens(db);
    if (current < 2) backfillOccupancyOnUpgrade(db);
    if (current < 3) invalidateCodexImportsForModelAttribution(db);
    if (current < 4) invalidateSessionFilesForSkillContext(db);
    db.pragma(`user_version = ${DATA_SCHEMA_VERSION}`);
  });
  run();
}

/**
 * v4 — ordered skill-context observations are projected from source session
 * files. Clear only already-associated Claude/Codex watcher hashes so normal
 * startup reparses each eligible file once.
 */
function invalidateSessionFilesForSkillContext(db: Database): void {
  const result = db.prepare(`
    UPDATE watched_files
    SET file_hash = ''
    WHERE file_path IN (
      SELECT file_path
      FROM browsing_sessions
      WHERE agent IN ('claude', 'codex')
        AND file_path IS NOT NULL
    )
  `).run();

  if (result.changes > 0) {
    console.log(`[migration] skill context: flagged ${result.changes} session file(s) for reparse`);
  }
}

/**
 * v3 — Codex JSONL now carries an authoritative model on each turn_context.
 * Older imports used the current config.toml model for every historical event.
 * Invalidate the event-import hashes once so normal auto-import reparses those
 * files; the duplicate-refresh path updates only events backed by an explicit
 * turn_context, leaving legacy config-only logs untouched.
 */
function invalidateCodexImportsForModelAttribution(db: Database): void {
  const result = db.prepare(`
    UPDATE import_state
    SET file_hash = ''
    WHERE source = 'codex'
  `).run();

  if (result.changes > 0) {
    console.log(`[migration] Codex model attribution: flagged ${result.changes} event import file(s) for refresh`);
  }
}

/**
 * v2 — Backfill context-window occupancy for sessions synced before the gauge
 * shipped. The gauge writes occupancy inside `insertParsedSession`, but the
 * watcher skips files whose hash is unchanged, so pre-existing idle Claude/Codex
 * sessions in an upgraded database would otherwise never populate. Invalidate
 * their `watched_files` hash once so the next startup sync reparses them and
 * fills the occupancy columns; already-populated sessions and non-live agents
 * (Antigravity is always null) are left alone. This is DB-only and cheap — the
 * actual reparse cost is deferred to the watcher's normal startup pass. The
 * version-counter guard makes it run exactly once, so genuinely-null sessions
 * (no usage) are re-parsed at most one extra time rather than on every boot.
 */
function backfillOccupancyOnUpgrade(db: Database): void {
  const result = db.prepare(`
    UPDATE watched_files
    SET file_hash = ''
    WHERE file_path IN (
      SELECT file_path FROM browsing_sessions
      WHERE agent IN ('claude', 'codex')
        AND context_used_tokens IS NULL
        AND file_path IS NOT NULL
    )
  `).run();

  if (result.changes > 0) {
    console.log(`[migration] occupancy backfill: flagged ${result.changes} session file(s) for reparse`);
  }
}

/**
 * v1 — Repair historical OpenAI/Codex events that stored `tokens_in` as a
 * cache-inclusive figure. Those rows billed the cached bulk at the full input
 * rate (often ~10x the cache-read rate), massively overstating Codex spend.
 * Re-normalize `tokens_in` to the uncached remainder and recompute `cost_usd`.
 * Anthropic rows already store net input and are left untouched.
 */
function backfillCacheInclusiveInputTokens(db: Database): void {
  const rows = db.prepare(`
    SELECT id, model, tokens_in, tokens_out, cache_read_tokens, cache_write_tokens
    FROM events
    WHERE model IS NOT NULL AND cache_read_tokens > 0
  `).all() as Array<{
    id: number;
    model: string;
    tokens_in: number;
    tokens_out: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
  }>;

  const update = db.prepare('UPDATE events SET tokens_in = ?, cost_usd = ? WHERE id = ?');
  let corrected = 0;

  // Runs inside the runDataMigrations transaction; the version-counter guard is
  // what makes this apply exactly once (the correction is not self-idempotent).
  for (const row of rows) {
    const provider = pricingRegistry.resolve(row.model)?.pricing.provider;
    // Only OpenAI/Google report cache-inclusive input; Anthropic is already net.
    if (provider !== 'openai' && provider !== 'google') continue;

    const netIn = Math.max(0, row.tokens_in - row.cache_read_tokens);
    if (netIn === row.tokens_in) continue; // nothing to subtract

    const cost = pricingRegistry.calculate(row.model, {
      input: netIn,
      output: row.tokens_out,
      cacheRead: row.cache_read_tokens,
      cacheWrite: row.cache_write_tokens,
    });
    update.run(netIn, cost, row.id);
    corrected++;
  }

  if (corrected > 0) {
    console.log(`[migration] cache-inclusive input fix: corrected ${corrected} OpenAI/Codex events`);
  }
}
