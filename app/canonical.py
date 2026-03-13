"""Canonical event store — reads/writes JSON events to the shared OneDrive root.

This module implements the storage contract from ALP-SHARED-MEMORY-SPEC.md.
The canonical store is the source of truth. Local SQLite is a cache only.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from app.config import config

# ---------------------------------------------------------------------------
# Event envelope builder
# ---------------------------------------------------------------------------

VALID_EVENT_TYPES = frozenset({
    "fact", "handoff", "loop_opened", "loop_closed",
    "project_context_updated", "decision_recorded",
})

# Secret-like patterns to reject before write (spec §10)
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret[_-]?key|password|token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)(sk-|pk_live_|pk_test_|ghp_|gho_|xox[bpsa]-)\S{10,}"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN pattern
]


def _now_utc() -> str:
    """UTC ISO-8601 timestamp with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_event_id() -> str:
    """UUID-like unique identifier for events."""
    return str(uuid.uuid4())


def reject_secrets(text: str) -> str | None:
    """Check text for secret-like material. Returns pattern description if found, else None."""
    for pat in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            return f"Rejected: text contains secret-like material matching pattern near '{m.group()[:20]}...'"
    return None


def build_event(
    event_type: str,
    kind: str,
    subject: str,
    summary: str,
    scope: str = "global",
    project_id: str | None = None,
    details: str | dict | None = None,
    tags: list[str] | None = None,
    recency_weight: float = 1.0,
    dedupe_key: str | None = None,
) -> dict:
    """Build a canonical event envelope per spec §4.2."""
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(f"Invalid event_type: {event_type}. Must be one of {VALID_EVENT_TYPES}")
    if scope not in ("global", "project"):
        raise ValueError(f"Invalid scope: {scope}. Must be 'global' or 'project'")
    if scope == "project" and not project_id:
        raise ValueError("project_id required when scope is 'project'")

    # Normalize tags
    normalized_tags = sorted(set(t.lower().strip() for t in (tags or [])))

    return {
        "event_id": _gen_event_id(),
        "created_at": _now_utc(),
        "machine_id": config.machine_id,
        "scope": scope,
        "project_id": project_id if scope == "project" else None,
        "event_type": event_type,
        "kind": kind,
        "subject": subject,
        "summary": summary,
        "details": details,
        "tags": normalized_tags,
        "recency_weight": recency_weight,
        "dedupe_key": dedupe_key,
    }


# ---------------------------------------------------------------------------
# Canonical store operations
# ---------------------------------------------------------------------------

def _event_dir(scope: str, project_id: str | None, created_at: str) -> Path:
    """Compute the directory path for a canonical event."""
    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    date_path = f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"

    if scope == "project" and project_id:
        return config.canonical_root / "projects" / project_id / "events" / date_path
    return config.canonical_root / "global" / "events" / date_path


def _event_filename(created_at: str, event_id: str) -> str:
    """Canonical event filename: <timestamp>-<event_id>.json."""
    # Convert 2026-03-12T22:05:00Z -> 20260312T220500Z
    ts = created_at.replace("-", "").replace(":", "")
    return f"{ts}-{event_id}.json"


def canonical_available() -> bool:
    """Check if the canonical store is reachable."""
    return config.canonical_root.exists()


def check_dedupe(scope: str, project_id: str | None, dedupe_key: str) -> bool:
    """Check if an event with the given dedupe_key already exists in the canonical store.

    Returns True if duplicate found (should skip write).
    Scans the scope's event directories. This is intentionally simple —
    for large stores, the cache should be consulted instead.
    """
    if scope == "project" and project_id:
        events_root = config.canonical_root / "projects" / project_id / "events"
    else:
        events_root = config.canonical_root / "global" / "events"

    if not events_root.exists():
        return False

    # Scan recent event files (last 90 days would be excessive — check all for correctness)
    for json_file in events_root.rglob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            if data.get("dedupe_key") == dedupe_key:
                return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


def write_canonical_event(event: dict) -> Path:
    """Write an event to the canonical store. Returns the file path written."""
    event_dir = _event_dir(event["scope"], event.get("project_id"), event["created_at"])
    event_dir.mkdir(parents=True, exist_ok=True)

    filename = _event_filename(event["created_at"], event["event_id"])
    filepath = event_dir / filename

    filepath.write_text(
        json.dumps(event, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info(f"Canonical event written: {filepath.name} type={event['event_type']}")
    return filepath


def read_canonical_events(
    scope: str = "global",
    project_id: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read canonical events, most recent first."""
    if scope == "project" and project_id:
        events_root = config.canonical_root / "projects" / project_id / "events"
    else:
        events_root = config.canonical_root / "global" / "events"

    if not events_root.exists():
        return []

    # Collect all event files, sorted by filename (timestamp) descending
    files = sorted(events_root.rglob("*.json"), reverse=True)

    results = []
    for f in files:
        if len(results) >= limit:
            break
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if event_type and data.get("event_type") != event_type:
                continue
            results.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return results


def read_all_canonical_events(
    scope: str | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Read ALL canonical events for cache rebuild. No limit."""
    results = []

    scopes_to_scan = []
    if scope is None or scope == "global":
        scopes_to_scan.append(("global", None))
    if scope is None or scope == "project":
        # Scan all projects or a specific one
        projects_root = config.canonical_root / "projects"
        if project_id:
            scopes_to_scan.append(("project", project_id))
        elif projects_root.exists():
            for p in projects_root.iterdir():
                if p.is_dir():
                    scopes_to_scan.append(("project", p.name))

    for s, pid in scopes_to_scan:
        if s == "project" and pid:
            events_root = config.canonical_root / "projects" / pid / "events"
        else:
            events_root = config.canonical_root / "global" / "events"

        if not events_root.exists():
            continue

        for f in events_root.rglob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                results.append(data)
            except (json.JSONDecodeError, OSError):
                continue

    results.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Snapshot writer (spec §5)
# ---------------------------------------------------------------------------

def write_snapshot(scope: str, project_id: str | None, name: str, content: str) -> Path:
    """Write a snapshot file (derived convenience, not source of truth)."""
    if scope == "project" and project_id:
        snap_dir = config.canonical_root / "projects" / project_id / "snapshots"
    else:
        snap_dir = config.canonical_root / "global" / "snapshots"

    snap_dir.mkdir(parents=True, exist_ok=True)
    filepath = snap_dir / name
    filepath.write_text(content, encoding="utf-8")
    logger.info(f"Snapshot written: {filepath}")
    return filepath


def write_handoff_snapshot(event: dict) -> Path:
    """Write latest-handoff.md from a handoff event."""
    content = (
        f"# Latest Handoff\n\n"
        f"- Created: {event['created_at']}\n"
        f"- Machine: {event['machine_id']}\n"
        f"- Summary: {event['summary']}\n"
    )
    if event.get("details"):
        details = event["details"]
        if isinstance(details, dict):
            if details.get("decisions"):
                content += f"- Decisions: {json.dumps(details['decisions'])}\n"
            if details.get("open_items"):
                content += f"- Open items: {json.dumps(details['open_items'])}\n"
            if details.get("next_steps"):
                content += f"- Next steps: {json.dumps(details['next_steps'])}\n"
            if details.get("context_notes"):
                content += f"- Context: {details['context_notes']}\n"
    return write_snapshot(
        event["scope"], event.get("project_id"), "latest-handoff.md", content
    )


def write_project_context_snapshot(event: dict) -> Path:
    """Write project-context.md from a project_context_updated event."""
    content = (
        f"# Project Context\n\n"
        f"- Updated: {event['created_at']}\n"
        f"- Machine: {event['machine_id']}\n"
        f"- Subject: {event['subject']}\n"
        f"- Summary: {event['summary']}\n"
    )
    if event.get("details"):
        if isinstance(event["details"], str):
            content += f"\n{event['details']}\n"
        else:
            content += f"\n{json.dumps(event['details'], indent=2)}\n"
    return write_snapshot(
        event["scope"], event.get("project_id"), "project-context.md", content
    )


# ---------------------------------------------------------------------------
# Outbox (spec §9) — offline/degraded writes
# ---------------------------------------------------------------------------

def outbox_write(event: dict) -> Path:
    """Queue an event to the local outbox when canonical store is unavailable."""
    config.outbox_dir.mkdir(parents=True, exist_ok=True)
    filename = _event_filename(event["created_at"], event["event_id"])
    filepath = config.outbox_dir / filename
    filepath.write_text(
        json.dumps(event, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.warning(f"Event queued to outbox (canonical unavailable): {filename}")
    return filepath


def outbox_count() -> int:
    """Count events waiting in the outbox."""
    if not config.outbox_dir.exists():
        return 0
    return len(list(config.outbox_dir.glob("*.json")))


def outbox_flush() -> int:
    """Flush outbox events to the canonical store. Returns count flushed."""
    if not config.outbox_dir.exists():
        return 0
    if not canonical_available():
        return 0

    flushed = 0
    for f in sorted(config.outbox_dir.glob("*.json")):
        try:
            event = json.loads(f.read_text(encoding="utf-8"))
            write_canonical_event(event)
            f.unlink()
            flushed += 1
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to flush outbox event {f.name}: {e}")
    if flushed:
        logger.info(f"Flushed {flushed} events from outbox to canonical store")
    return flushed


# ---------------------------------------------------------------------------
# Unified write with canonical-first, outbox-fallback
# ---------------------------------------------------------------------------

def store_event(event: dict) -> dict:
    """Write event to canonical store (or outbox if unavailable), then return the event.

    This is the primary write path. All tool writes go through here.
    """
    # Secret rejection (spec §10)
    for field in ("summary", "details", "subject"):
        val = event.get(field)
        if val and isinstance(val, str):
            rejection = reject_secrets(val)
            if rejection:
                return {"error": rejection, "event_id": event["event_id"]}

    # Dedupe check (spec §8.3)
    if event.get("dedupe_key") and canonical_available():
        if check_dedupe(event["scope"], event.get("project_id"), event["dedupe_key"]):
            logger.info(f"Dedupe: skipping event with key '{event['dedupe_key']}'")
            return {"skipped": True, "reason": "duplicate", "dedupe_key": event["dedupe_key"]}

    # Write to canonical or outbox
    if canonical_available():
        # Flush any pending outbox first
        outbox_flush()
        path = write_canonical_event(event)
        event["_canonical_path"] = str(path)
    else:
        path = outbox_write(event)
        event["_queued"] = True

    # Write snapshots for specific event types
    if event["event_type"] == "handoff":
        try:
            write_handoff_snapshot(event)
        except OSError as e:
            logger.error(f"Failed to write handoff snapshot: {e}")

    if event["event_type"] == "project_context_updated":
        try:
            write_project_context_snapshot(event)
        except OSError as e:
            logger.error(f"Failed to write project context snapshot: {e}")

    return event
