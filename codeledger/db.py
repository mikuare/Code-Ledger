from __future__ import annotations

import sqlite3
from pathlib import Path

# Tables and indexes are separated because they cannot be created at the same
# moment. `CREATE TABLE IF NOT EXISTS` is a no-op on a database that already has
# the table — including one created before a column existed — but
# `CREATE INDEX IF NOT EXISTS` is not: the index genuinely does not exist, so
# SQLite tries to build it and fails on the column the old table is missing.
# Indexes therefore have to wait until MIGRATIONS has added those columns. See
# `connect` for the ordering this enforces.
TABLES = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, language TEXT, size INTEGER, hash TEXT, mtime REAL, git_status TEXT, status TEXT NOT NULL DEFAULT 'current', last_analyzed TEXT, analysis_version TEXT, analysis_provider TEXT, coverage TEXT, last_modified_by TEXT, last_modified_session TEXT);
CREATE TABLE IF NOT EXISTS symbols (id INTEGER PRIMARY KEY, name TEXT NOT NULL, qualified_name TEXT, kind TEXT NOT NULL, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE, line_start INTEGER, line_end INTEGER, signature TEXT, hash TEXT, status TEXT NOT NULL DEFAULT 'active', created_at TEXT, updated_at TEXT, deleted_at TEXT, last_modified_by TEXT, last_modified_session TEXT, last_verified TEXT);
CREATE TABLE IF NOT EXISTS dependencies (id INTEGER PRIMARY KEY, source_file_id INTEGER REFERENCES files(id) ON DELETE CASCADE, source_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE, target_name TEXT NOT NULL, target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL, kind TEXT, UNIQUE(source_file_id, source_symbol_id, target_name, kind));
CREATE TABLE IF NOT EXISTS features (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT, status TEXT NOT NULL DEFAULT 'UNKNOWN', last_verified TEXT, last_changed TEXT);
CREATE TABLE IF NOT EXISTS agents (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, provider TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE NOT NULL, agent_id INTEGER REFERENCES agents(id), working_directory TEXT, start_time TEXT NOT NULL, end_time TEXT, request TEXT, result TEXT, status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, agent TEXT, session_id TEXT, user_request TEXT, summary TEXT, risk TEXT, git_commit TEXT, result TEXT, effect TEXT, files_added INTEGER DEFAULT 0, files_modified INTEGER DEFAULT 0, files_deleted INTEGER DEFAULT 0, symbols_added INTEGER DEFAULT 0, symbols_modified INTEGER DEFAULT 0, symbols_deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS change_files (change_id INTEGER REFERENCES changes(id) ON DELETE CASCADE, file_id INTEGER REFERENCES files(id) ON DELETE SET NULL, path TEXT NOT NULL, status TEXT, PRIMARY KEY(change_id, path));
CREATE TABLE IF NOT EXISTS change_symbols (change_id INTEGER REFERENCES changes(id) ON DELETE CASCADE, symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL, name TEXT NOT NULL, status TEXT, PRIMARY KEY(change_id, name));
CREATE TABLE IF NOT EXISTS issues (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT, status TEXT NOT NULL DEFAULT 'OPEN', severity TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, rationale TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifications (id INTEGER PRIMARY KEY, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, kind TEXT NOT NULL, result TEXT NOT NULL, evidence TEXT, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS git_commits (commit_hash TEXT PRIMARY KEY, parent_hash TEXT, author TEXT, timestamp TEXT, subject TEXT);
CREATE TABLE IF NOT EXISTS git_commit_files (commit_hash TEXT REFERENCES git_commits(commit_hash) ON DELETE CASCADE, path TEXT NOT NULL, status TEXT, PRIMARY KEY(commit_hash,path));
CREATE TABLE IF NOT EXISTS checkpoints (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL, agent TEXT, provider TEXT, model TEXT, model_version TEXT, goal TEXT NOT NULL, summary TEXT, current_state TEXT, next_action TEXT, git_commit TEXT, context_window INTEGER, context_used INTEGER, source TEXT NOT NULL DEFAULT 'agent', confidence TEXT NOT NULL DEFAULT 'HIGH', status TEXT NOT NULL DEFAULT 'OPEN', superseded_by INTEGER REFERENCES checkpoints(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS checkpoint_items (id INTEGER PRIMARY KEY, checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE, kind TEXT NOT NULL, ref_type TEXT, ref_id TEXT, text TEXT, ordinal INTEGER NOT NULL DEFAULT 0);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_files_coverage ON files(coverage);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_target_name ON dependencies(target_name);
CREATE INDEX IF NOT EXISTS idx_dependencies_target_id ON dependencies(target_symbol_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_source_file ON dependencies(source_file_id);
CREATE INDEX IF NOT EXISTS idx_changes_time ON changes(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_status ON checkpoints(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id);
CREATE INDEX IF NOT EXISTS idx_checkpoint_items_checkpoint ON checkpoint_items(checkpoint_id, kind, ordinal);
"""

# The whole schema, for callers that want to create a database in one statement.
# `connect` deliberately does not use this: it has to interleave the migrations.
SCHEMA = TABLES + INDEXES

MIGRATIONS: list[tuple[str, str, str]] = [
    ("files", "mtime_ns", "ALTER TABLE files ADD COLUMN mtime_ns INTEGER"),
    ("dependencies", "source_file_id", "ALTER TABLE dependencies ADD COLUMN source_file_id INTEGER REFERENCES files(id)"),
    ("files", "analysis_provider", "ALTER TABLE files ADD COLUMN analysis_provider TEXT"),
    ("files", "coverage", "ALTER TABLE files ADD COLUMN coverage TEXT"),
    ("changes", "effect", "ALTER TABLE changes ADD COLUMN effect TEXT"),
    # A session that ended by any route other than Ctrl+C used to stay 'active'
    # forever. Liveness needs evidence that outlives the process: which process
    # it was, on which machine, and when it was last known to be doing anything.
    ("sessions", "pid", "ALTER TABLE sessions ADD COLUMN pid INTEGER"),
    ("sessions", "host", "ALTER TABLE sessions ADD COLUMN host TEXT"),
    ("sessions", "last_activity_at", "ALTER TABLE sessions ADD COLUMN last_activity_at TEXT"),
    ("sessions", "last_heartbeat_at", "ALTER TABLE sessions ADD COLUMN last_heartbeat_at TEXT"),
    ("sessions", "status_reason", "ALTER TABLE sessions ADD COLUMN status_reason TEXT"),
    # Who a change is credited to is worth less than how well that is known.
    ("changes", "attribution_source", "ALTER TABLE changes ADD COLUMN attribution_source TEXT"),
    ("changes", "attribution_confidence", "ALTER TABLE changes ADD COLUMN attribution_confidence TEXT"),
    ("changes", "attribution_reason", "ALTER TABLE changes ADD COLUMN attribution_reason TEXT"),
    # Which model was driving an agent is per-session, not per-agent: the same
    # program runs a different model tomorrow. Recorded only when the runtime
    # actually says so, and left UNKNOWN otherwise — see agents.resolve_model.
    ("sessions", "provider", "ALTER TABLE sessions ADD COLUMN provider TEXT"),
    ("sessions", "model", "ALTER TABLE sessions ADD COLUMN model TEXT"),
    ("sessions", "model_version", "ALTER TABLE sessions ADD COLUMN model_version TEXT"),
]

def connect(root: Path) -> sqlite3.Connection:
    db_path = root / ".ai" / "codeledger" / "codeledger.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # `watch` and the MCP server routinely hold the database open at the same
    # time; WAL lets a reader proceed while the other process writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    # Order matters, and getting it wrong is not a hypothetical: a real database
    # created before `files.coverage` existed failed to open at all, because the
    # index on that column was created before the migration that adds it.
    #
    #   1. tables   — creates anything missing; a no-op for tables that exist,
    #                 whatever shape they are in
    #   2. migrate  — brings existing tables up to the current column set
    #   3. indexes  — every column an index names is now guaranteed to exist
    conn.executescript(TABLES)
    pending = [statement for table, column, statement in MIGRATIONS
               if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}]
    if pending:
        # One transaction for the whole upgrade. SQLite makes DDL transactional,
        # so a failure part-way leaves the database on its previous schema
        # rather than half-migrated. Nothing is caught and hidden here: a
        # migration that cannot be applied must surface, not leave a database
        # that looks upgraded and is not.
        conn.execute("BEGIN")
        try:
            for statement in pending:
                conn.execute(statement)
            # Older rows recorded dependencies without their owning file. Backfill
            # from the symbol so per-file cleanup can find them.
            conn.execute("UPDATE dependencies SET source_file_id=(SELECT file_id FROM symbols WHERE symbols.id=dependencies.source_symbol_id) WHERE source_file_id IS NULL AND source_symbol_id IS NOT NULL")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    conn.executescript(INDEXES)
    return conn
