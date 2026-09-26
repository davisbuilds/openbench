CREATE TABLE agents (
      id TEXT PRIMARY KEY,
      agent_type TEXT NOT NULL,
      name TEXT,
      registered_at TEXT NOT NULL DEFAULT (datetime('now')),
      last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
CREATE TABLE browsing_sessions (
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
    , context_used_tokens INTEGER, context_window_tokens INTEGER, project_identity TEXT, skill_context_capabilities_json TEXT
        CHECK (skill_context_capabilities_json IS NULL OR json_valid(skill_context_capabilities_json)));
CREATE TABLE events (
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
CREATE TABLE import_state (
      file_path TEXT PRIMARY KEY,
      file_hash TEXT NOT NULL,
      file_size INTEGER NOT NULL,
      source TEXT NOT NULL,
      events_imported INTEGER NOT NULL DEFAULT 0,
      imported_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
CREATE TABLE insights (
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
CREATE TABLE messages (
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
CREATE VIRTUAL TABLE messages_fts USING fts5(
      content,
      content=messages,
      content_rowid=id,
      tokenize='porter unicode61'
    );
CREATE TABLE otel_metrics (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      agent_type TEXT NOT NULL,
      metric_name TEXT NOT NULL,
      attrs TEXT,
      value REAL NOT NULL,
      temporality TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      client_timestamp TEXT
    );
CREATE TABLE pinned_messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      message_id INTEGER,
      message_ordinal INTEGER NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE(session_id, message_ordinal)
    );
CREATE TABLE provider_quotas (
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
CREATE TABLE session_catalog_observation_entries (
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
CREATE TABLE session_context_observations (
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
CREATE TABLE session_expected_skill_realizations (
      session_id TEXT PRIMARY KEY,
      realization_id TEXT NOT NULL,
      associated_at TEXT NOT NULL DEFAULT (datetime('now')),
      FOREIGN KEY(realization_id) REFERENCES skill_expected_realizations(id)
    );
CREATE TABLE session_items (
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
CREATE TABLE session_trace_summary (
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
CREATE TABLE session_turns (
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
CREATE TABLE sessions (
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
CREATE TABLE skill_catalog_snapshots (
      name TEXT NOT NULL,
      version TEXT,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      PRIMARY KEY (name, version)
    );
CREATE TABLE skill_expected_realizations (
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
CREATE TABLE tool_calls (
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
CREATE TABLE trace_quality_export_state (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      provider TEXT NOT NULL CHECK (provider IN ('langfuse')),
      local_trace_id TEXT NOT NULL,
      local_observation_id TEXT,
      external_trace_id TEXT,
      external_observation_id TEXT,
      payload_hash TEXT,
      status TEXT NOT NULL CHECK (status IN ('pending', 'exported', 'failed', 'skipped')),
      exported_at TEXT,
      error_message TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
CREATE TABLE watched_files (
      file_path TEXT PRIMARY KEY,
      file_hash TEXT NOT NULL,
      file_mtime TEXT,
      status TEXT NOT NULL DEFAULT 'parsed',
      last_parsed_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
CREATE INDEX idx_bs_agent ON browsing_sessions(agent);
CREATE INDEX idx_bs_ended_at ON browsing_sessions(ended_at DESC);
CREATE INDEX idx_bs_last_item_at ON browsing_sessions(last_item_at DESC);
CREATE INDEX idx_bs_live_status ON browsing_sessions(live_status);
CREATE INDEX idx_bs_project ON browsing_sessions(project);
CREATE INDEX idx_bs_started_at ON browsing_sessions(started_at);
CREATE INDEX idx_events_agent_type ON events(agent_type);
CREATE INDEX idx_events_benchmark_monitor
      ON events(source, session_id, agent_type, tool_name, model)
      WHERE source = 'benchmark';
CREATE INDEX idx_events_codex_import_usage_session_ts
      ON events(session_id, datetime(COALESCE(client_timestamp, created_at)))
      WHERE agent_type = 'codex'
        AND source = 'import'
        AND (
          COALESCE(cost_usd, 0) > 0
          OR COALESCE(tokens_in, 0) > 0
          OR COALESCE(tokens_out, 0) > 0
          OR COALESCE(cache_read_tokens, 0) > 0
          OR COALESCE(cache_write_tokens, 0) > 0
        );
CREATE INDEX idx_events_created_at ON events(created_at);
CREATE INDEX idx_events_created_at_order
      ON events(datetime(created_at) DESC, id DESC);
CREATE INDEX idx_events_created_model
      ON events(created_at, model, tokens_in, tokens_out, cost_usd);
CREATE INDEX idx_events_event_type ON events(event_type);
CREATE INDEX idx_events_model ON events(model);
CREATE INDEX idx_events_session_cost
      ON events(session_id, tokens_in, tokens_out, cost_usd);
CREATE INDEX idx_events_session_reconcile
      ON events(session_id, agent_type, source);
CREATE INDEX idx_events_study_id ON events(study_id) WHERE study_id IS NOT NULL;
CREATE INDEX idx_events_tool_name ON events(tool_name);
CREATE INDEX idx_events_usage_covering
      ON events(
        datetime(COALESCE(client_timestamp, created_at)),
        agent_type,
        source,
        session_id,
        project,
        model,
        client_timestamp,
        created_at,
        cost_usd,
        tokens_in,
        tokens_out,
        cache_read_tokens,
        cache_write_tokens
      )
      WHERE (source IS NULL OR source != 'benchmark')
        AND (
          COALESCE(cost_usd, 0) > 0
          OR COALESCE(tokens_in, 0) > 0
          OR COALESCE(tokens_out, 0) > 0
          OR COALESCE(cache_read_tokens, 0) > 0
          OR COALESCE(cache_write_tokens, 0) > 0
        );
CREATE INDEX idx_events_usage_ts
      ON events(datetime(COALESCE(client_timestamp, created_at)));
CREATE INDEX idx_insights_created_at ON insights(created_at DESC);
CREATE INDEX idx_insights_scope ON insights(kind, date_from, date_to, project, agent);
CREATE INDEX idx_messages_session_ordinal ON messages(session_id, ordinal);
CREATE INDEX idx_messages_session_role ON messages(session_id, role);
CREATE INDEX idx_otel_metrics_created ON otel_metrics(created_at DESC);
CREATE INDEX idx_otel_metrics_name ON otel_metrics(metric_name, created_at DESC);
CREATE INDEX idx_otel_metrics_session ON otel_metrics(session_id);
CREATE INDEX idx_pm_created_at ON pinned_messages(created_at DESC);
CREATE INDEX idx_pm_session_ordinal ON pinned_messages(session_id, message_ordinal);
CREATE INDEX idx_provider_quotas_updated_at ON provider_quotas(updated_at DESC);
CREATE INDEX idx_sco_session_kind_name_ordinal
      ON session_context_observations(session_id, kind, skill_name, ordinal);
CREATE INDEX idx_sco_skill_time
      ON session_context_observations(skill_name, observed_at);
CREATE INDEX idx_scoe_observation_ordinal
      ON session_catalog_observation_entries(observation_id, ordinal);
CREATE INDEX idx_scoe_skill
      ON session_catalog_observation_entries(skill_name);
CREATE INDEX idx_scs_name ON skill_catalog_snapshots(name);
CREATE INDEX idx_ser_harness_validity
      ON skill_expected_realizations(harness, valid_from, valid_to);
CREATE INDEX idx_sesr_realization
      ON session_expected_skill_realizations(realization_id);
CREATE INDEX idx_sessions_status ON sessions(status);
CREATE INDEX idx_si_session_created_at ON session_items(session_id, created_at, id);
CREATE INDEX idx_si_source_item_id ON session_items(source_item_id);
CREATE INDEX idx_si_turn_ordinal ON session_items(turn_id, ordinal);
CREATE INDEX idx_st_session_started_at ON session_turns(session_id, started_at DESC);
CREATE INDEX idx_st_source_turn_id ON session_turns(source_turn_id);
CREATE INDEX idx_sts_project ON session_trace_summary(project);
CREATE INDEX idx_sts_quality ON session_trace_summary(quality_score);
CREATE INDEX idx_sts_started ON session_trace_summary(started_at DESC);
CREATE INDEX idx_tc_category ON tool_calls(category);
CREATE INDEX idx_tc_session_id ON tool_calls(session_id);
CREATE INDEX idx_tc_tool_name ON tool_calls(tool_name);
CREATE INDEX idx_tq_export_external_trace ON trace_quality_export_state(provider, external_trace_id);
CREATE INDEX idx_tq_export_local_observation ON trace_quality_export_state(local_observation_id);
CREATE INDEX idx_tq_export_local_trace ON trace_quality_export_state(local_trace_id);
CREATE INDEX idx_tq_export_provider_status ON trace_quality_export_state(provider, status, exported_at DESC);
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
      END;
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
      END;
CREATE TRIGGER messages_fts_update AFTER UPDATE OF content ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
      END;
CREATE UNIQUE INDEX idx_sts_trace ON session_trace_summary(trace_id);
PRAGMA user_version=7;
