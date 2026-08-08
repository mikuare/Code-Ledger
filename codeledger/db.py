from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, language TEXT, size INTEGER, hash TEXT, mtime REAL, git_status TEXT, status TEXT NOT NULL DEFAULT 'current', last_analyzed TEXT, analysis_version TEXT, analysis_provider TEXT, coverage TEXT, last_modified_by TEXT, last_modified_session TEXT);
CREATE INDEX IF NOT EXISTS idx_files_coverage ON files(coverage);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);
CREATE TABLE IF NOT EXISTS symbols (id INTEGER PRIMARY KEY, name TEXT NOT NULL, qualified_name TEXT, kind TEXT NOT NULL, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE, line_start INTEGER, line_end INTEGER, signature TEXT, hash TEXT, status TEXT NOT NULL DEFAULT 'active', created_at TEXT, updated_at TEXT, deleted_at TEXT, last_modified_by TEXT, last_modified_session TEXT, last_verified TEXT);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE TABLE IF NOT EXISTS dependencies (id INTEGER PRIMARY KEY, source_file_id INTEGER REFERENCES files(id) ON DELETE CASCADE, source_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE, target_name TEXT NOT NULL, target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL, kind TEXT, UNIQUE(source_file_id, source_symbol_id, target_name, kind));
CREATE INDEX IF NOT EXISTS idx_dependencies_target_name ON dependencies(target_name);
CREATE INDEX IF NOT EXISTS idx_dependencies_target_id ON dependencies(target_symbol_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_source_file ON dependencies(source_file_id);
CREATE TABLE IF NOT EXISTS features (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT, status TEXT NOT NULL DEFAULT 'UNKNOWN', last_verified TEXT, last_changed TEXT);
CREATE TABLE IF NOT EXISTS agents (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, provider TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE NOT NULL, agent_id INTEGER REFERENCES agents(id), working_directory TEXT, start_time TEXT NOT NULL, end_time TEXT, request TEXT, result TEXT, status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, agent TEXT, session_id TEXT, user_request TEXT, summary TEXT, risk TEXT, git_commit TEXT, result TEXT, effect TEXT, files_added INTEGER DEFAULT 0, files_modified INTEGER DEFAULT 0, files_deleted INTEGER DEFAULT 0, symbols_added INTEGER DEFAULT 0, symbols_modified INTEGER DEFAULT 0, symbols_deleted INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_changes_time ON changes(timestamp DESC);
CREATE TABLE IF NOT EXISTS change_files (change_id INTEGER REFERENCES changes(id) ON DELETE CASCADE, file_id INTEGER REFERENCES files(id) ON DELETE SET NULL, path TEXT NOT NULL, status TEXT, PRIMARY KEY(change_id, path));
CREATE TABLE IF NOT EXISTS change_symbols (change_id INTEGER REFERENCES changes(id) ON DELETE CASCADE, symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL, name TEXT NOT NULL, status TEXT, PRIMARY KEY(change_id, name));
CREATE TABLE IF NOT EXISTS issues (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT, status TEXT NOT NULL DEFAULT 'OPEN', severity TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, rationale TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifications (id INTEGER PRIMARY KEY, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, kind TEXT NOT NULL, result TEXT NOT NULL, evidence TEXT, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS git_commits (commit_hash TEXT PRIMARY KEY, parent_hash TEXT, author TEXT, timestamp TEXT, subject TEXT);
CREATE TABLE IF NOT EXISTS git_commit_files (commit_hash TEXT REFERENCES git_commits(commit_hash) ON DELETE CASCADE, path TEXT NOT NULL, status TEXT, PRIMARY KEY(commit_hash,path));
"""

MIGRATIONS: list[tuple[str, str, str]] = [
    ("files", "mtime_ns", "ALTER TABLE files ADD COLUMN mtime_ns INTEGER"),
    ("dependencies", "source_file_id", "ALTER TABLE dependencies ADD COLUMN source_file_id INTEGER REFERENCES files(id)"),
    ("files", "analysis_provider", "ALTER TABLE files ADD COLUMN analysis_provider TEXT"),
    ("files", "coverage", "ALTER TABLE files ADD COLUMN coverage TEXT"),
    ("changes", "effect", "ALTER TABLE changes ADD COLUMN effect TEXT"),
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
    conn.executescript(SCHEMA)
    migrated = False
    for table, column, statement in MIGRATIONS:
        if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(statement)
            migrated = True
    if migrated:
        # Older rows recorded dependencies without their owning file. Backfill
        # from the symbol so per-file cleanup can find them.
        conn.execute("UPDATE dependencies SET source_file_id=(SELECT file_id FROM symbols WHERE symbols.id=dependencies.source_symbol_id) WHERE source_file_id IS NULL AND source_symbol_id IS NOT NULL")
        conn.commit()
    return conn
