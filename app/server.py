"""FastMCP server for Memory MCP — local-only persistent memory."""

import argparse
import asyncio
import sys
from typing import Any, Literal

from fastmcp import FastMCP
from loguru import logger

from app import __version__
from app.config import config
from app.db import init_db
from app import tools as tool_funcs

# Valid transport types
TransportType = Literal["stdio", "http"]
VALID_TRANSPORTS: list[str] = ["stdio", "http"]

# Configure logging
config.log_dir.mkdir(parents=True, exist_ok=True)
logger.add(
    config.log_dir / "server.log",
    rotation=config.log_rotation,
    retention=config.log_retention,
)

# Create FastMCP server
mcp: FastMCP[Any] = FastMCP(
    name="Memory MCP Server",
    instructions=(
        "Local-only persistent memory for Claude Code sessions. "
        "Stores preferences, decisions, corrections, handoffs, and open loops in SQLite. "
        "No network calls. No secrets. All data stays on this machine."
    ),
)


# ---------------------------------------------------------------------------
# Register tools on the FastMCP instance
# ---------------------------------------------------------------------------

@mcp.tool()
def memory_status() -> dict:
    """Check memory server health, database size, and memory counts by type."""
    return tool_funcs.memory_status()


@mcp.tool()
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
    return tool_funcs.memory_read_recent(memory_type, project_path, limit, include_archived)


@mcp.tool()
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
        project_path: Scope filter (NULL = global only, "*" = all).
        tags: JSON array of tags to filter by (e.g. '["tools", "python"]').
        limit: Max results (default 10, max 50).
    """
    return tool_funcs.memory_search(query, memory_type, project_path, tags, limit)


@mcp.tool()
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
        project_path: NULL = global scope.
        matter_name: Optional legal matter name.
        tags: JSON array of string tags (default '[]').
        confidence: high, medium, or low (default 'high').
        source: Origin — user, session, hook, migration (default 'user').
        supersedes_id: ID of memory this replaces (old one gets archived).
        session_id: Session that created this.
    """
    return tool_funcs.memory_write_fact(
        memory_type, content, summary, project_path, matter_name,
        tags, confidence, source, supersedes_id, session_id,
    )


@mcp.tool()
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
    return tool_funcs.memory_write_handoff(
        session_id, summary, decisions, open_items, next_steps,
        context_notes, project_path, matter_name,
    )


@mcp.tool()
def memory_get_open_loops(
    project_path: str | None = None,
    loop_type: str | None = None,
    priority: str | None = None,
    include_closed: bool = False,
    limit: int = 20,
) -> dict:
    """Retrieve open loops (unfinished tasks, unresolved questions, follow-ups, blockers).

    Args:
        project_path: NULL = global only, "*" = all scopes.
        loop_type: Filter by type (task, question, follow_up, blocker).
        priority: Filter by priority (high, normal, low).
        include_closed: Include closed loops (default false).
        limit: Max results (default 20).
    """
    return tool_funcs.memory_get_open_loops(project_path, loop_type, priority, include_closed, limit)


@mcp.tool()
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
        priority: high, normal, or low (default 'normal').
        project_path: Scope to a project.
        matter_name: Optional matter name.
        tags: JSON array of tags.
        session_id: Session creating this loop.
    """
    return tool_funcs.memory_create_loop(
        description, loop_type, priority, project_path,
        matter_name, tags, session_id,
    )


@mcp.tool()
def memory_close_loop(loop_id: str, resolution: str) -> dict:
    """Close an open loop with a resolution note.

    Args:
        loop_id: ID of the loop to close.
        resolution: How it was resolved.
    """
    return tool_funcs.memory_close_loop(loop_id, resolution)


@mcp.tool()
def memory_get_project_context(
    project_path: str,
    include_global: bool = True,
) -> dict:
    """Get all memories scoped to a project plus global, as a structured continuity brief.

    Returns the latest handoff, open loops, recent memories, and global preferences
    for the specified project.

    Args:
        project_path: The project directory path.
        include_global: Include global (unscoped) memories (default true).
    """
    return tool_funcs.memory_get_project_context(project_path, include_global)


@mcp.tool()
def memory_import_markdown(
    file_path: str,
    default_type: str = "observation",
    project_path: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Import existing markdown memory files (MEMORY.md) into the database.

    Parses bullet-point entries under headings. Idempotent — skips entries
    whose content already exists. Use dry_run=true to preview without writing.

    Args:
        file_path: Absolute path to the .md file.
        default_type: Default memory_type for entries without a recognized heading.
        project_path: Scope all imported entries to this project.
        dry_run: If true, parse but do not write.
    """
    return tool_funcs.memory_import_markdown(file_path, default_type, project_path, dry_run)


@mcp.tool()
def memory_read_codex(
    query: str | None = None,
    event_type: str | None = None,
    scope: str = "all",
    project_id: str | None = None,
    limit: int = 20,
) -> dict:
    """Read memories from Codex's local memory cache (read-only cross-environment bridge).

    Enables cross-environment memory sharing between Claude Code and Codex.
    Reads from Codex's cache.sqlite at ~/.codex/memory-cache/<machine>/cache.sqlite.

    Args:
        query: FTS5 search query (if None, returns recent events).
        event_type: Filter (fact, handoff, loop_opened, loop_closed, project_context_updated, decision_recorded).
        scope: 'global', 'project', or 'all' (default 'all').
        project_id: Filter by Codex project ID.
        limit: Max results (default 20, max 50).
    """
    return tool_funcs.memory_read_codex(query, event_type, scope, project_id, limit)


# ---------------------------------------------------------------------------
# CLI and main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Memory MCP Server")
    parser.add_argument(
        "-t", "--transport",
        choices=VALID_TRANSPORTS,
        default=None,
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=None,
        help="Port for HTTP transport (default: 3097)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Host to bind for HTTP (default: 127.0.0.1)",
    )
    return parser.parse_args()


def _is_broken_pipe_error(exc: BaseException) -> bool:
    if isinstance(exc, BrokenPipeError):
        return True
    if isinstance(exc, ExceptionGroup):
        return any(_is_broken_pipe_error(e) for e in exc.exceptions)
    return False


async def main() -> None:
    """Run the Memory MCP server."""
    init_db()

    args = parse_args()
    transport: str = args.transport or config.transport
    host = args.host or config.host
    port = args.port or config.port

    logger.info(f"Memory MCP Server v{__version__} starting (transport={transport})")

    try:
        if transport == "stdio":
            await mcp.run_async(transport="stdio")
        elif transport == "http":
            logger.info(f"Listening on http://{host}:{port}/mcp")
            await mcp.run_async(
                transport="streamable-http",
                host=host,
                port=port,
                path="/mcp",
            )
        else:
            logger.error(f"Invalid transport: {transport}")
            sys.exit(1)
    except BaseException as e:
        if _is_broken_pipe_error(e):
            logger.info("Client disconnected (broken pipe)")
            return
        logger.error(f"Server error: {e}")
        raise
