"""MCP tool implementations for Memory Server."""

import json
import os
import re
from datetime import datetime, timezone

from loguru import logger

from app.db import db_size_kb, get_connection


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_id() -> str:
    return os.urandom(16).hex()


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return dict(row)


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tool 1: memory_status
# ---------------------------------------------------------------------------

def memory_status() -> dict:
    """Check memory server health, database size, and memory counts by type."""
    conn = get_connection()
    try:
        # Counts by type
        rows = conn.execute(
            "SELECT memory_type, COUNT(*) as cnt FROM memories "
            "WHERE is_archived = 0 GROUP BY memory_type"
        ).fetchall()
        by_type = {r["memory_type"]: r["cnt"] for r in rows}
        total = sum(by_type.values())

        # Open loops
        open_loops = conn.execute(
            "SELECT COUNT(*) as cnt FROM open_loops WHERE status = 'open'"
        ).fetchone()["cnt"]

        # Total handoffs
        total_handoffs = conn.execute(
            "SELECT COUNT(*) as cnt FROM handoffs"
        ).fetchone()["cnt"]

        # Last handoff
        last = conn.execute(
            "SELECT id, session_id, summary, created_at FROM handoffs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        return {
            "status": "healthy",
            "version": "1.0.0",
            "db_path": str(get_connection().execute("PRAGMA database_list").fetchone()[2]),
            "db_size_kb": db_size_kb(),
            "counts": {
                "total_memories": total,
                "by_type": by_type,
                "open_loops": open_loops,
                "handoffs": total_handoffs,
            },
            "last_handoff": _row_to_dict(last) if last else None,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 2: memory_read_recent
# ---------------------------------------------------------------------------

def memory_read_recent(
    memory_type: str | None = None,
    project_path: str | None = None,
    limit: int = 20,
    include_archived: bool = False,
) -> dict:
    """Read recent memories, optionally filtered by type and/or project scope.

    Args:
        memory_type: Filter by type (preference, architecture_decision, project_context, correction, instruction, observation, handoff).
        project_path: NULL returns global-only, "*" returns all, specific path returns project-scoped + global.
        limit: Max results (default 20, max 100).
        include_archived: Include archived/superseded memories.
    """
    limit = min(limit, 100)
    conn = get_connection()
    try:
        conditions = []
        params: list = []

        if not include_archived:
            conditions.append("is_archived = 0")

        if memory_type:
            conditions.append("memory_type = ?")
            params.append(memory_type)

        if project_path is None:
            conditions.append("project_path IS NULL")
        elif project_path != "*":
            conditions.append("(project_path = ? OR project_path IS NULL)")
            params.append(project_path)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM memories WHERE {where} ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()

        # Count total matching
        count_sql = f"SELECT COUNT(*) as cnt FROM memories WHERE {where}"
        total = conn.execute(count_sql, params[:-1]).fetchone()["cnt"]

        return {
            "memories": _rows_to_list(rows),
            "total": total,
            "has_more": total > limit,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 3: memory_search
# ---------------------------------------------------------------------------

def memory_search(
    query: str,
    memory_type: str | None = None,
    project_path: str | None = None,
    tags: str | None = None,
    limit: int = 10,
) -> dict:
    """Full-text search across all memories using SQLite FTS5.

    Args:
        query: FTS5 query (supports AND, OR, NOT, "phrase", prefix*).
        memory_type: Filter by type.
        project_path: Scope filter.
        tags: JSON array of tags to filter by.
        limit: Max results (default 10, max 50).
    """
    limit = min(limit, 50)
    conn = get_connection()
    try:
        # Build join query: FTS match + filters on main table
        conditions = ["m.is_archived = 0"]
        params: list = [query]

        if memory_type:
            conditions.append("m.memory_type = ?")
            params.append(memory_type)

        if project_path is not None:
            if project_path == "*":
                pass
            else:
                conditions.append("(m.project_path = ? OR m.project_path IS NULL)")
                params.append(project_path)

        if tags:
            tag_list = json.loads(tags)
            for tag in tag_list:
                conditions.append("m.tags LIKE ?")
                params.append(f"%{tag}%")

        where = " AND ".join(conditions)
        params.append(limit)

        sql = f"""
            SELECT m.*, rank
            FROM memories_fts fts
            JOIN memories m ON m.rowid = fts.rowid
            WHERE memories_fts MATCH ? AND {where}
            ORDER BY rank
            LIMIT ?
        """

        rows = conn.execute(sql, params).fetchall()

        # Update accessed_at for returned results
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE memories SET accessed_at = ? WHERE id IN ({placeholders})",
                [_now()] + ids,
            )
            conn.commit()

        return {
            "results": _rows_to_list(rows),
            "query": query,
            "total_matches": len(rows),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 4: memory_write_fact
# ---------------------------------------------------------------------------

def memory_write_fact(
    memory_type: str,
    content: str,
    summary: str | None = None,
    project_path: str | None = None,
    matter_name: str | None = None,
    tags: str = "[]",
    confidence: str = "high",
    source: str = "user",
    supersedes_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Store a new memory (preference, decision, correction, observation, etc.).

    Args:
        memory_type: Required — preference, architecture_decision, project_context, correction, instruction, observation, handoff.
        content: The memory text.
        summary: Optional short summary for search results.
        project_path: NULL = global.
        matter_name: Optional legal matter name.
        tags: JSON array of string tags.
        confidence: high, medium, or low.
        source: Origin — user, session, hook, migration.
        supersedes_id: ID of memory this replaces (old one gets archived).
        session_id: Session that created this.
    """
    new_id = _gen_id()
    now = _now()
    conn = get_connection()
    try:
        superseded_info = None

        # Archive the old memory if superseding
        if supersedes_id:
            old = conn.execute(
                "SELECT id, content FROM memories WHERE id = ?", (supersedes_id,)
            ).fetchone()
            if old:
                conn.execute(
                    "UPDATE memories SET is_archived = 1, updated_at = ? WHERE id = ?",
                    (now, supersedes_id),
                )
                superseded_info = _row_to_dict(old)

        conn.execute(
            """INSERT INTO memories
            (id, memory_type, content, summary, source, project_path, matter_name,
             tags, confidence, created_at, updated_at, session_id, supersedes_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id, memory_type, content, summary, source,
                project_path, matter_name, tags, confidence,
                now, now, session_id, supersedes_id,
            ),
        )
        conn.commit()

        logger.info(f"Stored memory {new_id} type={memory_type}")
        return {
            "id": new_id,
            "memory_type": memory_type,
            "content": content,
            "created_at": now,
            "superseded": superseded_info,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 5: memory_write_handoff
# ---------------------------------------------------------------------------

def memory_write_handoff(
    session_id: str,
    summary: str,
    decisions: str = "[]",
    open_items: str = "[]",
    next_steps: str = "[]",
    context_notes: str | None = None,
    project_path: str | None = None,
    matter_name: str | None = None,
) -> dict:
    """Record a session handoff for continuity between sessions.

    Args:
        session_id: The session creating this handoff.
        summary: What was accomplished this session.
        decisions: JSON array of decisions made.
        open_items: JSON array of unfinished work.
        next_steps: JSON array of recommended next actions.
        context_notes: Anything the next session needs to know.
        project_path: Scope to a project.
        matter_name: Optional matter name.
    """
    new_id = _gen_id()
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO handoffs
            (id, session_id, summary, decisions, open_items, next_steps,
             context_notes, project_path, matter_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id, session_id, summary, decisions, open_items,
                next_steps, context_notes, project_path, matter_name, now,
            ),
        )
        conn.commit()

        logger.info(f"Stored handoff {new_id} for session {session_id}")
        return {
            "id": new_id,
            "session_id": session_id,
            "summary": summary,
            "created_at": now,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 6: memory_get_open_loops
# ---------------------------------------------------------------------------

def memory_get_open_loops(
    project_path: str | None = None,
    loop_type: str | None = None,
    priority: str | None = None,
    include_closed: bool = False,
    limit: int = 20,
) -> dict:
    """Retrieve open loops (unfinished tasks, unresolved questions, follow-ups, blockers).

    Args:
        project_path: NULL = global only, "*" = all.
        loop_type: Filter by type (task, question, follow_up, blocker).
        priority: Filter by priority (high, normal, low).
        include_closed: Include closed loops.
        limit: Max results (default 20).
    """
    limit = min(limit, 100)
    conn = get_connection()
    try:
        conditions: list[str] = []
        params: list = []

        if not include_closed:
            conditions.append("status = 'open'")

        if loop_type:
            conditions.append("loop_type = ?")
            params.append(loop_type)

        if priority:
            conditions.append("priority = ?")
            params.append(priority)

        if project_path is None:
            conditions.append("project_path IS NULL")
        elif project_path != "*":
            conditions.append("(project_path = ? OR project_path IS NULL)")
            params.append(project_path)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        sql = f"SELECT * FROM open_loops WHERE {where} ORDER BY created_at DESC LIMIT ?"
        rows = conn.execute(sql, params).fetchall()

        # Total open count (unfiltered)
        total_open = conn.execute(
            "SELECT COUNT(*) as cnt FROM open_loops WHERE status = 'open'"
        ).fetchone()["cnt"]

        return {
            "loops": _rows_to_list(rows),
            "total_open": total_open,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 7: memory_create_loop
# ---------------------------------------------------------------------------

def memory_create_loop(
    description: str,
    loop_type: str,
    priority: str = "normal",
    project_path: str | None = None,
    matter_name: str | None = None,
    tags: str = "[]",
    session_id: str | None = None,
) -> dict:
    """Create a new open loop (task, question, follow-up, or blocker).

    Args:
        description: What needs to be done or resolved.
        loop_type: task, question, follow_up, or blocker.
        priority: high, normal, or low.
        project_path: Scope to a project.
        matter_name: Optional matter name.
        tags: JSON array of tags.
        session_id: Session creating this loop.
    """
    new_id = _gen_id()
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO open_loops
            (id, description, loop_type, project_path, matter_name,
             status, priority, created_at, tags, session_id)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (
                new_id, description, loop_type, project_path,
                matter_name, priority, now, tags, session_id,
            ),
        )
        conn.commit()

        logger.info(f"Created loop {new_id} type={loop_type}")
        return {
            "id": new_id,
            "description": description,
            "loop_type": loop_type,
            "status": "open",
            "priority": priority,
            "created_at": now,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 8: memory_close_loop
# ---------------------------------------------------------------------------

def memory_close_loop(loop_id: str, resolution: str) -> dict:
    """Close an open loop with a resolution note.

    Args:
        loop_id: ID of the loop to close.
        resolution: How it was resolved.
    """
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE open_loops SET status = 'closed', resolution = ?, closed_at = ? WHERE id = ?",
            (resolution, now, loop_id),
        )
        conn.commit()

        row = conn.execute("SELECT * FROM open_loops WHERE id = ?", (loop_id,)).fetchone()
        if not row:
            return {"error": f"Loop {loop_id} not found"}

        logger.info(f"Closed loop {loop_id}")
        return _row_to_dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 9: memory_get_project_context
# ---------------------------------------------------------------------------

def memory_get_project_context(
    project_path: str,
    include_global: bool = True,
) -> dict:
    """Get all memories scoped to a project plus global, as a structured continuity brief.

    Args:
        project_path: The project directory path.
        include_global: Include global (unscoped) memories.
    """
    conn = get_connection()
    try:
        # Latest handoff for this project
        handoff = conn.execute(
            "SELECT * FROM handoffs WHERE project_path = ? ORDER BY created_at DESC LIMIT 1",
            (project_path,),
        ).fetchone()

        # Also check global handoff if no project-specific one
        if not handoff and include_global:
            handoff = conn.execute(
                "SELECT * FROM handoffs WHERE project_path IS NULL ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

        # Open loops for this project + global
        if include_global:
            loops = conn.execute(
                "SELECT * FROM open_loops WHERE status = 'open' "
                "AND (project_path = ? OR project_path IS NULL) "
                "ORDER BY priority DESC, created_at DESC LIMIT 20",
                (project_path,),
            ).fetchall()
        else:
            loops = conn.execute(
                "SELECT * FROM open_loops WHERE status = 'open' "
                "AND project_path = ? ORDER BY priority DESC, created_at DESC LIMIT 20",
                (project_path,),
            ).fetchall()

        # Recent memories for this project
        if include_global:
            memories = conn.execute(
                "SELECT * FROM memories WHERE is_archived = 0 "
                "AND (project_path = ? OR project_path IS NULL) "
                "ORDER BY updated_at DESC LIMIT 15",
                (project_path,),
            ).fetchall()
        else:
            memories = conn.execute(
                "SELECT * FROM memories WHERE is_archived = 0 "
                "AND project_path = ? ORDER BY updated_at DESC LIMIT 15",
                (project_path,),
            ).fetchall()

        # Global preferences (always useful)
        prefs = []
        if include_global:
            prefs = conn.execute(
                "SELECT * FROM memories WHERE is_archived = 0 "
                "AND memory_type = 'preference' AND project_path IS NULL "
                "ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()

        # Counts
        proj_mem = conn.execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE is_archived = 0 AND project_path = ?",
            (project_path,),
        ).fetchone()["cnt"]
        proj_loops = conn.execute(
            "SELECT COUNT(*) as cnt FROM open_loops WHERE status = 'open' AND project_path = ?",
            (project_path,),
        ).fetchone()["cnt"]
        proj_handoffs = conn.execute(
            "SELECT COUNT(*) as cnt FROM handoffs WHERE project_path = ?",
            (project_path,),
        ).fetchone()["cnt"]

        return {
            "project_path": project_path,
            "continuity_brief": {
                "latest_handoff": _row_to_dict(handoff) if handoff else None,
                "open_loops": _rows_to_list(loops),
                "recent_memories": _rows_to_list(memories),
                "global_preferences": _rows_to_list(prefs),
            },
            "counts": {
                "project_memories": proj_mem,
                "project_loops": proj_loops,
                "project_handoffs": proj_handoffs,
            },
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 10: memory_import_markdown
# ---------------------------------------------------------------------------

# Heading-to-type mapping for markdown import
_HEADING_TYPE_MAP = {
    "preferences": "preference",
    "preference": "preference",
    "patterns": "observation",
    "pattern": "observation",
    "corrections": "correction",
    "correction": "correction",
    "decisions": "architecture_decision",
    "decision": "architecture_decision",
    "architecture decisions": "architecture_decision",
    "architecture": "architecture_decision",
    "instructions": "instruction",
    "instruction": "instruction",
    "observations": "observation",
    "observation": "observation",
    "environment notes": "observation",
    "tool routing": "preference",
    "project context": "project_context",
}


def memory_import_markdown(
    file_path: str,
    default_type: str = "observation",
    project_path: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Import existing markdown memory files (MEMORY.md, checkpoint.md) into the database.

    Parses bullet-point entries under headings. Idempotent — skips entries whose
    content already exists in the database.

    Args:
        file_path: Absolute path to the .md file.
        default_type: Default memory_type for entries without a recognized heading.
        project_path: Scope all imported entries to this project.
        dry_run: If true, parse but do not write.
    """
    from pathlib import Path

    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    entries: list[dict] = []
    current_type = default_type
    current_heading = ""

    for line in lines:
        # Detect headings
        heading_match = re.match(r"^#{1,3}\s+(.+)$", line)
        if heading_match:
            heading_text = heading_match.group(1).strip().lower()
            current_heading = heading_text
            current_type = _HEADING_TYPE_MAP.get(heading_text, default_type)
            continue

        # Detect bullet points
        bullet_match = re.match(r"^\s*[-*]\s+\*\*(.+?)\*\*[:\s]*(.*)", line)
        if not bullet_match:
            bullet_match = re.match(r"^\s*[-*]\s+(.+)", line)

        if bullet_match:
            content = bullet_match.group(0).strip().lstrip("-* ").strip()
            if len(content) < 5:
                continue

            # Try to extract date from content
            date_match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", content)
            created_at = date_match.group(1) + "T00:00:00Z" if date_match else _now()

            entries.append({
                "content": content,
                "memory_type": current_type,
                "heading": current_heading,
                "created_at": created_at,
            })

    if dry_run:
        return {
            "file_path": file_path,
            "entries_found": len(entries),
            "entries_imported": 0,
            "entries_skipped": 0,
            "dry_run": True,
            "entries": entries[:50],  # Cap preview
        }

    conn = get_connection()
    imported = 0
    skipped = 0
    try:
        for entry in entries:
            # Check for duplicate content
            exists = conn.execute(
                "SELECT id FROM memories WHERE content = ? AND is_archived = 0",
                (entry["content"],),
            ).fetchone()

            if exists:
                skipped += 1
                entry["imported"] = False
                continue

            new_id = _gen_id()
            conn.execute(
                """INSERT INTO memories
                (id, memory_type, content, source, project_path,
                 created_at, updated_at, tags)
                VALUES (?, ?, ?, 'migration', ?, ?, ?, '[]')""",
                (
                    new_id, entry["memory_type"], entry["content"],
                    project_path, entry["created_at"], _now(),
                ),
            )
            imported += 1
            entry["imported"] = True
            entry["id"] = new_id

        conn.commit()
        logger.info(f"Imported {imported} entries from {file_path} (skipped {skipped})")

        return {
            "file_path": file_path,
            "entries_found": len(entries),
            "entries_imported": imported,
            "entries_skipped": skipped,
            "entries": entries[:50],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 11: memory_read_codex — read-only access to Codex's memory cache
# ---------------------------------------------------------------------------

def _find_codex_cache_db() -> str | None:
    """Locate the Codex memory-cache SQLite database.

    Codex stores its memory cache at:
        ~/.codex/memory-cache/<machine_id>/cache.sqlite

    Returns the first cache.sqlite found, or None.
    """
    from pathlib import Path

    cache_root = Path.home() / ".codex" / "memory-cache"
    if not cache_root.exists():
        return None

    for machine_dir in cache_root.iterdir():
        db_path = machine_dir / "cache.sqlite"
        if db_path.exists():
            return str(db_path)
    return None


def _codex_row_to_dict(row) -> dict:
    """Convert a Codex cache SQLite row to a clean dict."""
    if row is None:
        return {}
    d = dict(row)
    # Parse JSON fields
    for json_field, target in [("details_json", "details"), ("tags_json", "tags")]:
        if json_field in d:
            try:
                d[target] = json.loads(d.pop(json_field) or "null")
            except (json.JSONDecodeError, TypeError):
                d[target] = d.pop(json_field)
    return d


def memory_read_codex(
    query: str | None = None,
    event_type: str | None = None,
    scope: str = "all",
    project_id: str | None = None,
    limit: int = 20,
) -> dict:
    """Read memories from Codex's local memory cache (read-only cross-environment bridge).

    Provides read access to events stored by the Codex memory MCP server,
    enabling cross-environment memory continuity between Claude Code and Codex.

    Args:
        query: FTS5 search query (if None, returns recent events).
        event_type: Filter by type (fact, handoff, loop_opened, loop_closed,
                    project_context_updated, decision_recorded).
        scope: 'global', 'project', or 'all' (default 'all').
        project_id: Filter by project ID (used with scope='project' or 'all').
        limit: Max results (default 20, max 50).
    """
    import sqlite3 as _sqlite3

    limit = min(limit, 50)
    db_path = _find_codex_cache_db()
    if not db_path:
        return {
            "error": "Codex memory cache not found at ~/.codex/memory-cache/",
            "hint": "Ensure the codex-memory-mcp server has been run at least once.",
            "results": [],
            "total_matches": 0,
        }

    conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = _sqlite3.Row
    try:
        # Check if FTS is available
        fts_enabled = False
        try:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = 'fts_enabled'"
            ).fetchone()
            fts_enabled = row and row["value"] == "true"
        except _sqlite3.OperationalError:
            pass

        if query and fts_enabled:
            # FTS search
            clauses = ["events_fts MATCH ?"]
            params: list = [query]

            if event_type:
                clauses.append("e.event_type = ?")
                params.append(event_type)

            if scope == "global":
                clauses.append("e.scope = 'global'")
            elif scope == "project":
                clauses.append("e.scope = 'project'")
                if project_id:
                    clauses.append("e.project_id = ?")
                    params.append(project_id)
            elif scope == "all" and project_id:
                clauses.append(
                    "(e.scope = 'global' OR (e.scope = 'project' AND e.project_id = ?))"
                )
                params.append(project_id)

            params.append(limit)
            sql = f"""
                SELECT e.*, bm25(events_fts) AS rank
                FROM events_fts
                JOIN events e ON e.event_id = events_fts.event_id
                WHERE {' AND '.join(clauses)}
                ORDER BY rank, e.created_at DESC
                LIMIT ?
            """
            rows = conn.execute(sql, params).fetchall()
        elif query:
            # LIKE fallback if FTS unavailable
            like = f"%{query}%"
            clauses_like = [
                "(subject LIKE ? OR summary LIKE ? OR details_json LIKE ?)"
            ]
            params_like: list = [like, like, like]

            if event_type:
                clauses_like.append("event_type = ?")
                params_like.append(event_type)

            if scope == "global":
                clauses_like.append("scope = 'global'")
            elif scope == "project":
                clauses_like.append("scope = 'project'")
                if project_id:
                    clauses_like.append("project_id = ?")
                    params_like.append(project_id)
            elif scope == "all" and project_id:
                clauses_like.append(
                    "(scope = 'global' OR (scope = 'project' AND project_id = ?))"
                )
                params_like.append(project_id)

            params_like.append(limit)
            sql = f"""
                SELECT * FROM events
                WHERE {' AND '.join(clauses_like)}
                ORDER BY created_at DESC LIMIT ?
            """
            rows = conn.execute(sql, params_like).fetchall()
        else:
            # No query — return recent events
            clauses_recent: list[str] = []
            params_recent: list = []

            if event_type:
                clauses_recent.append("event_type = ?")
                params_recent.append(event_type)

            if scope == "global":
                clauses_recent.append("scope = 'global'")
            elif scope == "project":
                clauses_recent.append("scope = 'project'")
                if project_id:
                    clauses_recent.append("project_id = ?")
                    params_recent.append(project_id)
            elif scope == "all" and project_id:
                clauses_recent.append(
                    "(scope = 'global' OR (scope = 'project' AND project_id = ?))"
                )
                params_recent.append(project_id)

            where = f"WHERE {' AND '.join(clauses_recent)}" if clauses_recent else ""
            params_recent.append(limit)
            sql = f"SELECT * FROM events {where} ORDER BY created_at DESC LIMIT ?"
            rows = conn.execute(sql, params_recent).fetchall()

        results = [_codex_row_to_dict(r) for r in rows]

        # Total count
        total = conn.execute("SELECT COUNT(*) as cnt FROM events").fetchone()["cnt"]

        return {
            "source": "codex-memory-cache",
            "db_path": db_path,
            "total_codex_events": total,
            "total_matches": len(results),
            "results": results,
        }
    except _sqlite3.OperationalError as e:
        return {
            "error": f"Failed to read Codex cache: {e}",
            "db_path": db_path,
            "results": [],
            "total_matches": 0,
        }
    finally:
        conn.close()
