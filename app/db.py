"""SQLite database layer for Memory MCP Server."""

import sqlite3
from pathlib import Path

from loguru import logger

from app.config import config

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Core memory table
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    memory_type     TEXT NOT NULL CHECK (memory_type IN (
        'preference',
        'architecture_decision',
        'project_context',
        'correction',
        'instruction',
        'observation',
        'handoff'
    )),
    content         TEXT NOT NULL,
    summary         TEXT,
    source          TEXT DEFAULT 'user',
    project_path    TEXT,
    matter_name     TEXT,
    tags            TEXT DEFAULT '[]',
    confidence      TEXT DEFAULT 'high' CHECK (confidence IN ('high', 'medium', 'low')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    accessed_at     TEXT,
    expires_at      TEXT,
    is_archived     INTEGER DEFAULT 0,
    session_id      TEXT,
    supersedes_id   TEXT REFERENCES memories(id)
);

-- Open loops table
CREATE TABLE IF NOT EXISTS open_loops (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    description     TEXT NOT NULL,
    loop_type       TEXT NOT NULL CHECK (loop_type IN (
        'task', 'question', 'follow_up', 'blocker'
    )),
    project_path    TEXT,
    matter_name     TEXT,
    status          TEXT DEFAULT 'open' CHECK (status IN ('open', 'closed', 'stale')),
    priority        TEXT DEFAULT 'normal' CHECK (priority IN ('high', 'normal', 'low')),
    resolution      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    closed_at       TEXT,
    tags            TEXT DEFAULT '[]',
    session_id      TEXT
);

-- Session handoffs table
CREATE TABLE IF NOT EXISTS handoffs (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    session_id      TEXT NOT NULL,
    summary         TEXT NOT NULL,
    decisions       TEXT DEFAULT '[]',
    open_items      TEXT DEFAULT '[]',
    next_steps      TEXT DEFAULT '[]',
    context_notes   TEXT,
    project_path    TEXT,
    matter_name     TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type) WHERE is_archived = 0;
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_path) WHERE is_archived = 0;
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_open_loops_status ON open_loops(status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_open_loops_project ON open_loops(project_path) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_handoffs_session ON handoffs(session_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_project ON handoffs(project_path);
CREATE INDEX IF NOT EXISTS idx_handoffs_created ON handoffs(created_at DESC);

-- FTS5 for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    summary,
    tags,
    content=memories,
    content_rowid=rowid
);

-- FTS sync triggers
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, summary, tags)
    VALUES (new.rowid, new.content, new.summary, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary, tags)
    VALUES ('delete', old.rowid, old.content, old.summary, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary, tags)
    VALUES ('delete', old.rowid, old.content, old.summary, old.tags);
    INSERT INTO memories_fts(rowid, content, summary, tags)
    VALUES (new.rowid, new.content, new.summary, new.tags);
END;
"""


def get_connection() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and row factory."""
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Initialize the database schema. Idempotent."""
    conn = get_connection()
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {config.db_path}")


def db_size_kb() -> float:
    """Get database file size in KB."""
    path = Path(config.db_path)
    if path.exists():
        return round(path.stat().st_size / 1024, 1)
    return 0.0
