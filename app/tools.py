"""MCP tool implementations for Memory Server.

Write path: build canonical event → store_event() → also insert into SQLite cache.
Read path: read from SQLite cache (populated from canonical events).
"""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

from loguru import logger

from app import __version__

from app.canonical import (
    build_event,
    canonical_available,
    outbox_count,
    store_event,
)
from app.config import config
from app.db import (
    cache_close_loop,
    cache_insert_handoff,
    cache_insert_loop,
    cache_insert_memory,
    db_size_kb,
    get_connection,
    rebuild_cache_from_canonical,
)
from app.identity import resolve_project_id


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


def _memory_type_to_event_type(memory_type: str) -> str:
    """Map Claude memory_type to canonical event_type."""
    if memory_type == "architecture_decision":
        return "decision_recorded"
    if memory_type == "project_context":
        return "project_context_updated"
    return "fact"


# ---------------------------------------------------------------------------
# Tool 1: memory_status
# ---------------------------------------------------------------------------

def memory_status() -> dict:
    """Check memory server health, database size, and memory counts by type."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT memory_type, COUNT(*) as cnt FROM memories "
            "WHERE is_archived = 0 GROUP BY memory_type"
        ).fetchall()
        by_type = {r["memory_type"]: r["cnt"] for r in rows}
        total = sum(by_type.values())

        open_loops = conn.execute(
            "SELECT COUNT(*) as cnt FROM open_loops WHERE status = 'open'"
        ).fetchone()["cnt"]

        total_handoffs = conn.execute(
            "SELECT COUNT(*) as cnt FROM handoffs"
        ).fetchone()["cnt"]

        last = conn.execute(
            "SELECT id, session_id, summary, created_at FROM handoffs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        # Sync metadata
        last_rebuild = None
        try:
            row = conn.execute(
                "SELECT value FROM sync_meta WHERE key = 'last_rebuild'"
            ).fetchone()
            if row:
                last_rebuild = row["value"]
        except Exception:
            pass

        return {
            "status": "healthy",
            "version": __version__,
            "machine_id": config.machine_id,
            "memory_enabled": config.memory_enabled,
            "canonical_root": str(config.canonical_root),
            "canonical_available": canonical_available(),
            "cache_db_path": str(config.db_path),
            "cache_db_size_kb": db_size_kb(),
            "outbox_count": outbox_count(),
            "last_cache_rebuild": last_rebuild,
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
# Tool 2: memory_read_recent (reads from cache)
# ---------------------------------------------------------------------------

def memory_read_recent(
    memory_type: str | None = None,
    project_path: str | None = None,
    limit: int = 20,
    include_archived: bool = False,
) -> dict:
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
# Tool 3: memory_search (reads from cache FTS5)
# ---------------------------------------------------------------------------

def _sanitize_fts5_query(query: str) -> str:
    """Escape FTS5 special characters so hyphens, UUIDs, etc. search correctly.

    FTS5 interprets bare hyphens as NOT operators. Quote each whitespace-
    delimited token that contains a hyphen so FTS5 treats it as literal text.
    """
    tokens = query.split()
    safe = []
    for t in tokens:
        if "-" in t and not t.startswith('"'):
            safe.append(f'"{t}"')
        else:
            safe.append(t)
    return " ".join(safe)


def memory_search(
    query: str,
    memory_type: str | None = None,
    project_path: str | None = None,
    tags: str | None = None,
    limit: int = 10,
) -> dict:
    limit = min(limit, 50)
    fts_query = _sanitize_fts5_query(query)
    conn = get_connection()
    try:
        conditions = ["m.is_archived = 0"]
        params: list = [fts_query]

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
# Tool 4: memory_write_fact (dual-write: canonical + cache)
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
    if not config.memory_enabled:
        return {"error": "Memory is disabled", "memory_enabled": False}

    tag_list = json.loads(tags) if isinstance(tags, str) else tags
    event_type = _memory_type_to_event_type(memory_type)
    project_id = resolve_project_id(project_path)
    scope = "project" if project_path else "global"

    # Build dedupe key from full content hash — prevents exact duplicates
    # without aliasing distinct facts that share opening text
    dedupe_key = f"{memory_type}:{hashlib.sha256(content.encode()).hexdigest()}" if content else None

    # Build canonical event
    event = build_event(
        event_type=event_type,
        kind=memory_type,
        subject=summary or content[:80],
        summary=content,
        scope=scope,
        project_id=project_id,
        details={
            "source": source,
            "confidence": confidence,
            "project_path": project_path,
            "matter_name": matter_name,
            "session_id": session_id,
            "supersedes_id": supersedes_id,
        },
        tags=tag_list,
        dedupe_key=dedupe_key,
    )

    # Write to canonical store (or outbox)
    result = store_event(event)
    if result.get("error") or result.get("skipped"):
        return result

    # Also insert into SQLite cache
    new_id = event["event_id"]
    conn = get_connection()
    try:
        cache_insert_memory(
            conn,
            id=new_id,
            event_id=new_id,
            memory_type=memory_type,
            content=content,
            summary=summary,
            source=source,
            project_path=project_path,
            matter_name=matter_name,
            tags=tags,
            confidence=confidence,
            created_at=event["created_at"],
            session_id=session_id,
            supersedes_id=supersedes_id,
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Stored memory {new_id} type={memory_type}")
    response = {
        "id": new_id,
        "event_id": new_id,
        "memory_type": memory_type,
        "content": content,
        "created_at": event["created_at"],
        "canonical": not result.get("_queued", False),
    }
    if supersedes_id:
        response["superseded"] = {"id": supersedes_id, "archived": True}
    return response


# ---------------------------------------------------------------------------
# Tool 5: memory_write_handoff (dual-write: canonical + cache)
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
    if not config.memory_enabled:
        return {"error": "Memory is disabled", "memory_enabled": False}

    project_id = resolve_project_id(project_path)
    scope = "project" if project_path else "global"

    decisions_list = json.loads(decisions) if isinstance(decisions, str) else decisions
    open_items_list = json.loads(open_items) if isinstance(open_items, str) else open_items
    next_steps_list = json.loads(next_steps) if isinstance(next_steps, str) else next_steps

    # Build handoff event
    event = build_event(
        event_type="handoff",
        kind="session_handoff",
        subject=f"handoff-{session_id}",
        summary=summary,
        scope=scope,
        project_id=project_id,
        details={
            "session_id": session_id,
            "decisions": decisions_list,
            "open_items": open_items_list,
            "next_steps": next_steps_list,
            "context_notes": context_notes,
            "project_path": project_path,
            "matter_name": matter_name,
        },
        tags=["handoff"],
    )

    result = store_event(event)
    if result.get("error") or result.get("skipped"):
        return result

    # Insert into cache
    new_id = event["event_id"]
    conn = get_connection()
    try:
        cache_insert_handoff(
            conn,
            id=new_id,
            event_id=new_id,
            session_id=session_id,
            summary=summary,
            decisions=decisions,
            open_items=open_items,
            next_steps=next_steps,
            context_notes=context_notes,
            project_path=project_path,
            matter_name=matter_name,
            created_at=event["created_at"],
        )
        conn.commit()

        # Emit decision_recorded events for each decision (spec §6.5)
        for decision in decisions_list:
            dec_text = decision if isinstance(decision, str) else json.dumps(decision)
            dec_event = build_event(
                event_type="decision_recorded",
                kind="architecture_decision",
                subject=dec_text[:80],
                summary=dec_text,
                scope=scope,
                project_id=project_id,
                tags=["handoff", "decision"],
            )
            store_event(dec_event)

        # Emit loop_opened events for each open item (spec §6.5)
        for item in open_items_list:
            item_text = item if isinstance(item, str) else json.dumps(item)
            loop_subject = str(uuid.uuid4())
            loop_event = build_event(
                event_type="loop_opened",
                kind="task",
                subject=loop_subject,
                summary=item_text,
                scope=scope,
                project_id=project_id,
                details={
                    "loop_type": "task",
                    "priority": "normal",
                    "project_path": project_path,
                    "session_id": session_id,
                },
                tags=["handoff", "open-item"],
            )
            loop_result = store_event(loop_event)
            if not loop_result.get("error") and not loop_result.get("skipped"):
                cache_insert_loop(
                    conn,
                    id=loop_event["event_id"],
                    event_id=loop_event["event_id"],
                    canonical_subject=loop_subject,
                    description=item_text,
                    loop_type="task",
                    project_path=project_path,
                    priority="normal",
                    created_at=loop_event["created_at"],
                    session_id=session_id,
                )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Stored handoff {new_id} for session {session_id}")
    return {
        "id": new_id,
        "event_id": new_id,
        "session_id": session_id,
        "summary": summary,
        "created_at": event["created_at"],
        "canonical": not result.get("_queued", False),
    }


# ---------------------------------------------------------------------------
# Tool 6: memory_get_open_loops (reads from cache)
# ---------------------------------------------------------------------------

def memory_get_open_loops(
    project_path: str | None = None,
    loop_type: str | None = None,
    priority: str | None = None,
    include_closed: bool = False,
    limit: int = 20,
) -> dict:
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
# Tool 7: memory_create_loop (dual-write: canonical + cache)
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
    if not config.memory_enabled:
        return {"error": "Memory is disabled", "memory_enabled": False}

    tag_list = json.loads(tags) if isinstance(tags, str) else tags
    project_id = resolve_project_id(project_path)
    scope = "project" if project_path else "global"

    # Stable loop identity — UUID, not truncated prose
    loop_subject = str(uuid.uuid4())

    event = build_event(
        event_type="loop_opened",
        kind=loop_type,
        subject=loop_subject,
        summary=description,
        scope=scope,
        project_id=project_id,
        details={
            "loop_type": loop_type,
            "priority": priority,
            "project_path": project_path,
            "matter_name": matter_name,
            "session_id": session_id,
        },
        tags=tag_list + [loop_type],
    )

    result = store_event(event)
    if result.get("error") or result.get("skipped"):
        return result

    new_id = event["event_id"]
    canonical_subject = loop_subject
    conn = get_connection()
    try:
        cache_insert_loop(
            conn,
            id=new_id,
            event_id=new_id,
            canonical_subject=canonical_subject,
            description=description,
            loop_type=loop_type,
            project_path=project_path,
            matter_name=matter_name,
            priority=priority,
            tags=tags,
            created_at=event["created_at"],
            session_id=session_id,
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Created loop {new_id} type={loop_type}")
    return {
        "id": new_id,
        "event_id": new_id,
        "description": description,
        "loop_type": loop_type,
        "status": "open",
        "priority": priority,
        "created_at": event["created_at"],
        "canonical": not result.get("_queued", False),
    }


# ---------------------------------------------------------------------------
# Tool 8: memory_close_loop (emit loop_closed event + update cache)
# ---------------------------------------------------------------------------

def memory_close_loop(loop_id: str, resolution: str) -> dict:
    """Close an open loop by emitting a loop_closed event (append-only)."""
    if not config.memory_enabled:
        return {"error": "Memory is disabled", "memory_enabled": False}

    # Look up the loop in cache to get context
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM open_loops WHERE id = ?", (loop_id,)).fetchone()
        if not row:
            return {"error": f"Loop {loop_id} not found"}

        loop_data = _row_to_dict(row)
        project_path = loop_data.get("project_path")
        project_id = resolve_project_id(project_path)
        scope = "project" if project_path else "global"

        # Use the canonical subject from the loop_opened event as the loop
        # identifier in the loop_closed event. This ensures cross-runtime
        # compatibility — both Codex and Claude match loops by this field.
        canonical_subj = loop_data.get("canonical_subject") or loop_id

        # Build loop_closed event (spec §6.7 — new event, not mutation)
        event = build_event(
            event_type="loop_closed",
            kind=loop_data.get("loop_type", "task"),
            subject=canonical_subj,  # Must match loop_opened.subject
            summary=resolution,
            scope=scope,
            project_id=project_id,
            details={
                "loop_id": canonical_subj,
                "original_description": loop_data.get("description"),
                "resolution": resolution,
            },
            tags=["loop-closed"],
        )

        result = store_event(event)
        if result.get("error"):
            return result

        # Update cache (local mutation is fine — cache is not source of truth)
        cache_close_loop(
            conn,
            loop_id=loop_id,
            resolution=resolution,
            closed_at=event["created_at"],
            close_event_id=event["event_id"],
        )
        conn.commit()

        # Re-fetch for return
        updated = conn.execute("SELECT * FROM open_loops WHERE id = ?", (loop_id,)).fetchone()
        logger.info(f"Closed loop {loop_id}")
        return _row_to_dict(updated)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 9: memory_get_project_context (reads from cache)
# ---------------------------------------------------------------------------

def memory_get_project_context(
    project_path: str,
    include_global: bool = True,
) -> dict:
    conn = get_connection()
    try:
        handoff = conn.execute(
            "SELECT * FROM handoffs WHERE project_path = ? ORDER BY created_at DESC LIMIT 1",
            (project_path,),
        ).fetchone()

        if not handoff and include_global:
            handoff = conn.execute(
                "SELECT * FROM handoffs WHERE project_path IS NULL ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

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

        prefs = []
        if include_global:
            prefs = conn.execute(
                "SELECT * FROM memories WHERE is_archived = 0 "
                "AND memory_type = 'preference' AND project_path IS NULL "
                "ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()

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
    from pathlib import Path

    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    entries: list[dict] = []
    current_type = default_type

    for line in lines:
        heading_match = re.match(r"^#{1,3}\s+(.+)$", line)
        if heading_match:
            heading_text = heading_match.group(1).strip().lower()
            current_type = _HEADING_TYPE_MAP.get(heading_text, default_type)
            continue

        bullet_match = re.match(r"^\s*[-*]\s+\*\*(.+?)\*\*[:\s]*(.*)", line)
        if not bullet_match:
            bullet_match = re.match(r"^\s*[-*]\s+(.+)", line)

        if bullet_match:
            content = bullet_match.group(0).strip().lstrip("-* ").strip()
            if len(content) < 5:
                continue

            date_match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", content)
            created_at = date_match.group(1) + "T00:00:00Z" if date_match else _now()

            entries.append({
                "content": content,
                "memory_type": current_type,
                "created_at": created_at,
            })

    if dry_run:
        return {
            "file_path": file_path,
            "entries_found": len(entries),
            "entries_imported": 0,
            "entries_skipped": 0,
            "dry_run": True,
            "entries": entries[:50],
        }

    imported = 0
    skipped = 0
    conn = get_connection()
    try:
        for entry in entries:
            # Check for duplicate in cache
            exists = conn.execute(
                "SELECT id FROM memories WHERE content = ? AND is_archived = 0",
                (entry["content"],),
            ).fetchone()

            if exists:
                skipped += 1
                entry["imported"] = False
                continue

            # Write as canonical event + cache
            result = memory_write_fact(
                memory_type=entry["memory_type"],
                content=entry["content"],
                source="migration",
                project_path=project_path,
            )

            if result.get("error"):
                skipped += 1
                entry["imported"] = False
            else:
                imported += 1
                entry["imported"] = True
                entry["id"] = result.get("id")

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
# Tool 11: memory_read_codex (compatibility bridge — reads Codex's local cache)
# ---------------------------------------------------------------------------

def _find_codex_cache_db() -> str | None:
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
    if row is None:
        return {}
    d = dict(row)
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
    """Read from Codex's local memory cache (compatibility bridge).

    NOTE: Once both agents use the same canonical store, this tool becomes
    redundant. Both read from the same canonical events via the shared cache.
    """
    import sqlite3 as _sqlite3

    limit = min(limit, 50)
    db_path = _find_codex_cache_db()
    if not db_path:
        return {
            "error": "Codex memory cache not found at ~/.codex/memory-cache/",
            "hint": "Both agents now share the canonical store. Use memory_search instead.",
            "results": [],
            "total_matches": 0,
        }

    conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = _sqlite3.Row
    try:
        fts_enabled = False
        try:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = 'fts_enabled'"
            ).fetchone()
            fts_enabled = row and row["value"] == "true"
        except _sqlite3.OperationalError:
            pass

        if query and fts_enabled:
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
                clauses.append("(e.scope = 'global' OR (e.scope = 'project' AND e.project_id = ?))")
                params.append(project_id)
            params.append(limit)
            sql = f"""
                SELECT e.*, bm25(events_fts) AS rank
                FROM events_fts
                JOIN events e ON e.event_id = events_fts.event_id
                WHERE {' AND '.join(clauses)}
                ORDER BY rank, e.created_at DESC LIMIT ?
            """
            rows = conn.execute(sql, params).fetchall()
        elif query:
            like = f"%{query}%"
            clauses_like = ["(subject LIKE ? OR summary LIKE ? OR details_json LIKE ?)"]
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
                clauses_like.append("(scope = 'global' OR (scope = 'project' AND project_id = ?))")
                params_like.append(project_id)
            params_like.append(limit)
            sql = f"SELECT * FROM events WHERE {' AND '.join(clauses_like)} ORDER BY created_at DESC LIMIT ?"
            rows = conn.execute(sql, params_like).fetchall()
        else:
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
                clauses_recent.append("(scope = 'global' OR (scope = 'project' AND project_id = ?))")
                params_recent.append(project_id)
            where = f"WHERE {' AND '.join(clauses_recent)}" if clauses_recent else ""
            params_recent.append(limit)
            sql = f"SELECT * FROM events {where} ORDER BY created_at DESC LIMIT ?"
            rows = conn.execute(sql, params_recent).fetchall()

        results = [_codex_row_to_dict(r) for r in rows]
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


# ---------------------------------------------------------------------------
# Tool 12: memory_set_enabled (spec §6.2)
# ---------------------------------------------------------------------------

def memory_set_enabled(enabled: bool) -> dict:
    """Enable or disable persistent memory writes. State persists across restarts."""
    config.memory_enabled = enabled
    config.persist_enabled_state()
    logger.info(f"Memory {'enabled' if enabled else 'disabled'} (persisted)")
    return {
        "memory_enabled": config.memory_enabled,
        "message": f"Persistent memory {'enabled' if enabled else 'disabled'} (persisted to disk)",
    }


# ---------------------------------------------------------------------------
# Tool 13: memory_resume_context (spec §6.3)
# ---------------------------------------------------------------------------

def memory_resume_context(
    project_path: str | None = None,
) -> dict:
    """Build a continuity payload for fresh-session resume.

    Returns the latest handoff, open loops, project context,
    recent preferences, recent decisions, and recent unresolved questions.
    """
    conn = get_connection()
    try:
        project_cond = "project_path IS NULL" if not project_path else "(project_path = ? OR project_path IS NULL)"
        project_params = [] if not project_path else [project_path]

        # Latest handoff
        if project_path:
            handoff = conn.execute(
                "SELECT * FROM handoffs WHERE project_path = ? ORDER BY created_at DESC LIMIT 1",
                (project_path,),
            ).fetchone()
            if not handoff:
                handoff = conn.execute(
                    "SELECT * FROM handoffs WHERE project_path IS NULL ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
        else:
            handoff = conn.execute(
                "SELECT * FROM handoffs WHERE project_path IS NULL ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

        # Open loops
        loops = conn.execute(
            f"SELECT * FROM open_loops WHERE status = 'open' AND {project_cond} "
            "ORDER BY priority DESC, created_at DESC LIMIT 20",
            project_params,
        ).fetchall()

        # Recent preferences
        prefs = conn.execute(
            "SELECT * FROM memories WHERE is_archived = 0 AND memory_type = 'preference' "
            f"AND {project_cond} ORDER BY updated_at DESC LIMIT 10",
            project_params,
        ).fetchall()

        # Recent decisions
        decisions = conn.execute(
            "SELECT * FROM memories WHERE is_archived = 0 AND memory_type = 'architecture_decision' "
            f"AND {project_cond} ORDER BY updated_at DESC LIMIT 10",
            project_params,
        ).fetchall()

        # Unresolved questions
        questions = conn.execute(
            f"SELECT * FROM open_loops WHERE status = 'open' AND loop_type = 'question' AND {project_cond} "
            "ORDER BY created_at DESC LIMIT 10",
            project_params,
        ).fetchall()

        # Recent corrections
        corrections = conn.execute(
            "SELECT * FROM memories WHERE is_archived = 0 AND memory_type = 'correction' "
            f"AND {project_cond} ORDER BY updated_at DESC LIMIT 5",
            project_params,
        ).fetchall()

        return {
            "project_path": project_path,
            "canonical_available": canonical_available(),
            "continuity": {
                "latest_handoff": _row_to_dict(handoff) if handoff else None,
                "open_loops": _rows_to_list(loops),
                "preferences": _rows_to_list(prefs),
                "decisions": _rows_to_list(decisions),
                "unresolved_questions": _rows_to_list(questions),
                "recent_corrections": _rows_to_list(corrections),
            },
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 14: memory_checkpoint (spec §6.9)
# ---------------------------------------------------------------------------

def memory_checkpoint(
    session_id: str,
    task_summary: str,
    decisions: str = "[]",
    completed_work: str = "[]",
    open_loops: str = "[]",
    next_step: str | None = None,
    project_path: str | None = None,
    matter_name: str | None = None,
) -> dict:
    """Structured checkpoint — convenience wrapper over memory_write_handoff.

    Captures task summary, decisions, completed work, open loops, and next step.
    """
    # Build context notes from completed work and next step
    completed_list = json.loads(completed_work) if isinstance(completed_work, str) else completed_work
    context_parts = []
    if completed_list:
        context_parts.append("Completed: " + "; ".join(
            c if isinstance(c, str) else json.dumps(c) for c in completed_list
        ))
    if next_step:
        context_parts.append(f"Next step: {next_step}")

    return memory_write_handoff(
        session_id=session_id,
        summary=task_summary,
        decisions=decisions,
        open_items=open_loops,
        next_steps=json.dumps([next_step] if next_step else []),
        context_notes="; ".join(context_parts) if context_parts else None,
        project_path=project_path,
        matter_name=matter_name,
    )
