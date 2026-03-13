"""SQLite database layer for Memory MCP Server.

This is a LOCAL CACHE only — the canonical store is the source of truth.
The cache provides fast reads and FTS5 search. It is rebuildable from
canonical events at any time.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from app.config import config

SCHEMA_VERSION = 2

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Core memory table (cache of fact/decision/project_context events)
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    event_id        TEXT,
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

-- Open loops table (cache of loop_opened/loop_closed events)
CREATE TABLE IF NOT EXISTS open_loops (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    event_id        TEXT,
    canonical_subject TEXT,
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

-- Session handoffs table (cache of handoff events)
CREATE TABLE IF NOT EXISTS handoffs (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    event_id        TEXT,
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

-- Sync metadata
CREATE TABLE IF NOT EXISTS sync_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
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

# SQL to add event_id column to existing tables (migration)
_MIGRATION_SQL = [
    "ALTER TABLE memories ADD COLUMN event_id TEXT",
    "ALTER TABLE open_loops ADD COLUMN event_id TEXT",
    "ALTER TABLE handoffs ADD COLUMN event_id TEXT",
    "ALTER TABLE open_loops ADD COLUMN canonical_subject TEXT",
    "CREATE INDEX IF NOT EXISTS idx_memories_event ON memories(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_open_loops_event ON open_loops(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_event ON handoffs(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_open_loops_canonical_subject ON open_loops(canonical_subject)",
    "CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT)",
]


def get_connection() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and row factory."""
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Initialize the database schema. Idempotent. Runs migrations."""
    conn = get_connection()
    conn.executescript(SCHEMA_SQL)
    conn.commit()

    # Run migrations for existing databases
    for sql in _MIGRATION_SQL:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # Column/table already exists
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {config.db_path}")


def db_size_kb() -> float:
    """Get database file size in KB."""
    path = Path(config.db_path)
    if path.exists():
        return round(path.stat().st_size / 1024, 1)
    return 0.0


# ---------------------------------------------------------------------------
# Stale-detection and auto-sync
# ---------------------------------------------------------------------------

def _newest_canonical_event_mtime() -> float | None:
    """Return the mtime of the newest canonical event file, or None if empty.

    Walks the canonical event directories and finds the most recently modified
    .json file. Uses os.scandir for speed — avoids reading file contents.
    """
    events_roots = []
    global_events = config.canonical_root / "global" / "events"
    if global_events.exists():
        events_roots.append(global_events)
    projects_root = config.canonical_root / "projects"
    if projects_root.exists():
        for p in projects_root.iterdir():
            pe = p / "events"
            if pe.exists():
                events_roots.append(pe)

    newest = 0.0
    for root in events_roots:
        for dirpath, _dirnames, filenames in root.walk():
            for fn in filenames:
                if fn.endswith(".json"):
                    try:
                        st = (dirpath / fn).stat()
                        if st.st_mtime > newest:
                            newest = st.st_mtime
                    except OSError:
                        continue
    return newest if newest > 0 else None


def _last_rebuild_timestamp() -> float | None:
    """Return the last_rebuild timestamp from sync_meta as epoch seconds, or None.

    Prefers the high-precision epoch float stored by the rebuild.
    Falls back to parsing the ISO string (second precision).
    """
    try:
        conn = get_connection()
        # Prefer epoch float (sub-second precision)
        row = conn.execute(
            "SELECT value FROM sync_meta WHERE key = 'last_rebuild_epoch'"
        ).fetchone()
        if row and row["value"]:
            conn.close()
            return float(row["value"])
        # Fallback to ISO string
        row = conn.execute(
            "SELECT value FROM sync_meta WHERE key = 'last_rebuild'"
        ).fetchone()
        conn.close()
        if row and row["value"]:
            dt = datetime.fromisoformat(row["value"].replace("Z", "+00:00"))
            return dt.timestamp()
    except Exception:
        pass
    return None


def cache_freshness() -> dict:
    """Check whether the cache is stale relative to the canonical store.

    Returns a dict with:
      - cache_fresh: bool
      - last_cache_rebuild: str | None (ISO timestamp)
      - last_canonical_event_mtime: str | None (ISO timestamp)
      - sync_needed: bool
    """
    newest_mtime = _newest_canonical_event_mtime()
    rebuild_ts = _last_rebuild_timestamp()

    # Convert to ISO strings for display
    newest_iso = None
    if newest_mtime:
        newest_iso = datetime.fromtimestamp(newest_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    rebuild_iso = None
    if rebuild_ts:
        rebuild_iso = datetime.fromtimestamp(rebuild_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    sync_needed = False
    if newest_mtime is not None:
        if rebuild_ts is None:
            sync_needed = True
        elif newest_mtime > rebuild_ts:
            sync_needed = True

    return {
        "cache_fresh": not sync_needed,
        "last_cache_rebuild": rebuild_iso,
        "last_canonical_event_mtime": newest_iso,
        "sync_needed": sync_needed,
    }


def ensure_cache_fresh() -> dict:
    """If the cache is stale, rebuild from canonical events. Fail-soft.

    Returns a dict with sync result info. On failure, returns cache_fresh=False
    and the error message — never raises.
    """
    try:
        freshness = cache_freshness()
        if not freshness["sync_needed"]:
            return {"synced": False, "reason": "already fresh", **freshness}

        logger.info("Cache stale — rebuilding from canonical events")
        result = rebuild_cache_from_canonical()
        if result.get("rebuilt"):
            return {
                "synced": True,
                "events_replayed": result.get("total_events", 0),
                **cache_freshness(),  # Re-check after rebuild
            }
        return {
            "synced": False,
            "reason": result.get("reason", "rebuild returned no data"),
            **freshness,
        }
    except Exception as e:
        logger.error(f"Auto-sync failed: {e}")
        return {
            "synced": False,
            "cache_fresh": False,
            "sync_error": str(e),
        }


# ---------------------------------------------------------------------------
# Cache operations — insert canonical events into SQLite cache
# ---------------------------------------------------------------------------

def cache_insert_memory(
    conn: sqlite3.Connection,
    *,
    id: str,
    event_id: str,
    memory_type: str,
    content: str,
    summary: str | None = None,
    source: str = "user",
    project_path: str | None = None,
    matter_name: str | None = None,
    tags: str = "[]",
    confidence: str = "high",
    created_at: str,
    session_id: str | None = None,
    supersedes_id: str | None = None,
) -> None:
    """Insert a memory into the cache from a canonical event."""
    # Archive old memory if superseding
    if supersedes_id:
        conn.execute(
            "UPDATE memories SET is_archived = 1, updated_at = ? WHERE id = ?",
            (created_at, supersedes_id),
        )

    conn.execute(
        """INSERT OR IGNORE INTO memories
        (id, event_id, memory_type, content, summary, source, project_path,
         matter_name, tags, confidence, created_at, updated_at, session_id, supersedes_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, event_id, memory_type, content, summary, source, project_path,
         matter_name, tags, confidence, created_at, created_at, session_id, supersedes_id),
    )


def cache_insert_loop(
    conn: sqlite3.Connection,
    *,
    id: str,
    event_id: str,
    description: str,
    loop_type: str,
    canonical_subject: str | None = None,
    project_path: str | None = None,
    matter_name: str | None = None,
    priority: str = "normal",
    tags: str = "[]",
    created_at: str,
    session_id: str | None = None,
) -> None:
    """Insert an open loop into the cache from a loop_opened event."""
    conn.execute(
        """INSERT OR IGNORE INTO open_loops
        (id, event_id, canonical_subject, description, loop_type, project_path,
         matter_name, status, priority, created_at, tags, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
        (id, event_id, canonical_subject, description, loop_type, project_path,
         matter_name, priority, created_at, tags, session_id),
    )


def cache_close_loop(
    conn: sqlite3.Connection,
    *,
    loop_id: str,
    resolution: str,
    closed_at: str,
    close_event_id: str,
) -> None:
    """Mark a loop as closed in the cache from a loop_closed event."""
    conn.execute(
        "UPDATE open_loops SET status = 'closed', resolution = ?, closed_at = ? WHERE id = ?",
        (resolution, closed_at, loop_id),
    )


def cache_insert_handoff(
    conn: sqlite3.Connection,
    *,
    id: str,
    event_id: str,
    session_id: str,
    summary: str,
    decisions: str = "[]",
    open_items: str = "[]",
    next_steps: str = "[]",
    context_notes: str | None = None,
    project_path: str | None = None,
    matter_name: str | None = None,
    created_at: str,
) -> None:
    """Insert a handoff into the cache from a handoff event."""
    conn.execute(
        """INSERT OR IGNORE INTO handoffs
        (id, event_id, session_id, summary, decisions, open_items, next_steps,
         context_notes, project_path, matter_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, event_id, session_id, summary, decisions, open_items, next_steps,
         context_notes, project_path, matter_name, created_at),
    )


# ---------------------------------------------------------------------------
# Full cache rebuild from canonical events
# ---------------------------------------------------------------------------

def rebuild_cache_from_canonical() -> dict:
    """Rebuild the entire SQLite cache from canonical events.

    Clears all cache tables and re-populates from the canonical store.
    Returns stats about what was rebuilt.
    """
    from app.canonical import read_all_canonical_events

    events = read_all_canonical_events()
    if not events:
        return {"rebuilt": False, "reason": "no canonical events found", "counts": {}}

    conn = get_connection()
    try:
        # Clear cache tables
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM open_loops")
        conn.execute("DELETE FROM handoffs")
        conn.execute("DELETE FROM memories_fts")

        counts = {"fact": 0, "handoff": 0, "loop_opened": 0, "loop_closed": 0,
                  "decision_recorded": 0, "project_context_updated": 0}

        # Sort events chronologically, with secondary ordering to ensure
        # creates replay before closes when timestamps are equal
        _type_order = {
            "fact": 0, "decision_recorded": 0, "project_context_updated": 0,
            "handoff": 1, "loop_opened": 2, "loop_closed": 3,
        }
        events.sort(key=lambda e: (
            e.get("created_at", ""),
            _type_order.get(e.get("event_type"), 0),
        ))

        for event in events:
            etype = event.get("event_type")
            counts[etype] = counts.get(etype, 0) + 1
            _replay_event_to_cache(conn, event)

        conn.commit()

        # Update sync timestamps — store both ISO (for display) and epoch
        # float (for precise stale-detection comparison)
        from app.canonical import _now_utc
        import time
        conn.execute(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_rebuild', ?)",
            (_now_utc(),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_rebuild_epoch', ?)",
            (str(time.time()),),
        )
        conn.commit()

        logger.info(f"Cache rebuilt from {len(events)} canonical events")
        return {"rebuilt": True, "total_events": len(events), "counts": counts}
    finally:
        conn.close()


def _replay_event_to_cache(conn: sqlite3.Connection, event: dict) -> None:
    """Replay a single canonical event into the cache tables."""
    etype = event.get("event_type")
    details = event.get("details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except (json.JSONDecodeError, TypeError):
            pass

    if etype == "fact":
        _cache_fact_event(conn, event, details)
    elif etype == "decision_recorded":
        _cache_decision_event(conn, event, details)
    elif etype == "project_context_updated":
        _cache_project_context_event(conn, event, details)
    elif etype == "handoff":
        _cache_handoff_event(conn, event, details)
    elif etype == "loop_opened":
        _cache_loop_opened_event(conn, event, details)
    elif etype == "loop_closed":
        _cache_loop_closed_event(conn, event, details)


def _cache_fact_event(conn: sqlite3.Connection, event: dict, details) -> None:
    kind = event.get("kind", "observation")
    memory_type = _kind_to_memory_type(kind)
    tags_str = json.dumps(event.get("tags", []))
    cache_insert_memory(
        conn,
        id=event["event_id"],
        event_id=event["event_id"],
        memory_type=memory_type,
        content=event.get("summary", ""),
        summary=event.get("subject"),
        source=details.get("source", "user") if isinstance(details, dict) else "user",
        project_path=details.get("project_path") if isinstance(details, dict) else None,
        matter_name=details.get("matter_name") if isinstance(details, dict) else None,
        tags=tags_str,
        confidence=details.get("confidence", "high") if isinstance(details, dict) else "high",
        created_at=event["created_at"],
        session_id=details.get("session_id") if isinstance(details, dict) else None,
        supersedes_id=details.get("supersedes_id") if isinstance(details, dict) else None,
    )


def _cache_decision_event(conn: sqlite3.Connection, event: dict, details) -> None:
    tags_str = json.dumps(event.get("tags", []))
    cache_insert_memory(
        conn,
        id=event["event_id"],
        event_id=event["event_id"],
        memory_type="architecture_decision",
        content=event.get("summary", ""),
        summary=event.get("subject"),
        project_path=details.get("project_path") if isinstance(details, dict) else None,
        tags=tags_str,
        created_at=event["created_at"],
    )


def _cache_project_context_event(conn: sqlite3.Connection, event: dict, details) -> None:
    tags_str = json.dumps(event.get("tags", []))
    cache_insert_memory(
        conn,
        id=event["event_id"],
        event_id=event["event_id"],
        memory_type="project_context",
        content=event.get("summary", ""),
        summary=event.get("subject"),
        project_path=details.get("project_path") if isinstance(details, dict) else None,
        tags=tags_str,
        created_at=event["created_at"],
    )


def _cache_handoff_event(conn: sqlite3.Connection, event: dict, details) -> None:
    if not isinstance(details, dict):
        details = {}
    cache_insert_handoff(
        conn,
        id=event["event_id"],
        event_id=event["event_id"],
        session_id=details.get("session_id", "unknown"),
        summary=event.get("summary", ""),
        decisions=json.dumps(details.get("decisions", [])),
        open_items=json.dumps(details.get("open_items", [])),
        next_steps=json.dumps(details.get("next_steps", [])),
        context_notes=details.get("context_notes"),
        project_path=details.get("project_path"),
        matter_name=details.get("matter_name"),
        created_at=event["created_at"],
    )


def _cache_loop_opened_event(conn: sqlite3.Connection, event: dict, details) -> None:
    if not isinstance(details, dict):
        details = {}
    tags_str = json.dumps(event.get("tags", []))
    cache_insert_loop(
        conn,
        id=event["event_id"],
        event_id=event["event_id"],
        canonical_subject=event.get("subject"),
        description=event.get("summary", ""),
        loop_type=details.get("loop_type", "task"),
        project_path=details.get("project_path"),
        matter_name=details.get("matter_name"),
        priority=details.get("priority", "normal"),
        tags=tags_str,
        created_at=event["created_at"],
        session_id=details.get("session_id"),
    )


def _cache_loop_closed_event(conn: sqlite3.Connection, event: dict, details) -> None:
    if not isinstance(details, dict):
        details = {}
    # The canonical loop identifier is in the loop_closed event's subject field.
    # Try matching by canonical_subject first (cross-runtime compatible),
    # then fall back to matching by id (backward compat with pre-v2.0 data).
    close_subject = event.get("subject", "")
    loop_id = details.get("loop_id", close_subject)

    # Try canonical_subject match first
    row = conn.execute(
        "SELECT id FROM open_loops WHERE canonical_subject = ? AND status = 'open'",
        (close_subject,),
    ).fetchone()

    if row:
        loop_id = row["id"]

    cache_close_loop(
        conn,
        loop_id=loop_id,
        resolution=event.get("summary", ""),
        closed_at=event["created_at"],
        close_event_id=event["event_id"],
    )


def _kind_to_memory_type(kind: str) -> str:
    """Map canonical event kind to SQLite memory_type."""
    mapping = {
        "preference": "preference",
        "architecture_decision": "architecture_decision",
        "project_context": "project_context",
        "correction": "correction",
        "instruction": "instruction",
        "observation": "observation",
        "handoff": "handoff",
    }
    return mapping.get(kind, "observation")
