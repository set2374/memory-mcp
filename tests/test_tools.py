"""Tests for MCP tool implementations."""

import json
import tempfile
from pathlib import Path

from app.tools import (
    memory_close_loop,
    memory_create_loop,
    memory_get_open_loops,
    memory_get_project_context,
    memory_import_markdown,
    memory_read_codex,
    memory_read_recent,
    memory_search,
    memory_status,
    memory_write_fact,
    memory_write_handoff,
)


def test_memory_status(temp_db):
    result = memory_status()
    assert result["status"] == "healthy"
    assert result["version"] == "2.2.0"
    assert result["counts"]["total_memories"] == 0
    assert result["counts"]["open_loops"] == 0
    assert result["last_handoff"] is None


def test_write_fact_and_read_back(temp_db):
    # Write
    result = memory_write_fact(
        memory_type="preference",
        content="Always use bun instead of npm",
        tags='["tools", "js"]',
    )
    assert result["id"]
    assert result["memory_type"] == "preference"

    # Read back
    recent = memory_read_recent(memory_type="preference")
    assert recent["total"] == 1
    assert recent["memories"][0]["content"] == "Always use bun instead of npm"


def test_write_fact_with_supersedes(temp_db):
    # Write original
    orig = memory_write_fact(
        memory_type="preference",
        content="Use npm for packages",
    )

    # Supersede
    new = memory_write_fact(
        memory_type="preference",
        content="Use bun for packages (faster)",
        supersedes_id=orig["id"],
    )

    assert new["superseded"]["id"] == orig["id"]

    # Original should be archived
    recent = memory_read_recent(memory_type="preference")
    assert recent["total"] == 1
    assert recent["memories"][0]["content"] == "Use bun for packages (faster)"


def test_search_fts(temp_db):
    memory_write_fact(memory_type="observation", content="SQLite FTS5 supports phrase queries")
    memory_write_fact(memory_type="observation", content="Python asyncio event loop basics")

    results = memory_search("SQLite FTS5")
    assert results["total_matches"] >= 1
    assert "SQLite" in results["results"][0]["content"]


def test_search_with_type_filter(temp_db):
    memory_write_fact(memory_type="preference", content="Use dark theme everywhere")
    memory_write_fact(memory_type="observation", content="Dark theme reduces eye strain")

    results = memory_search("dark theme", memory_type="preference")
    assert results["total_matches"] == 1
    assert results["results"][0]["memory_type"] == "preference"


def test_search_with_project_filter(temp_db):
    memory_write_fact(memory_type="observation", content="Project A uses React", project_path="/proj/a")
    memory_write_fact(memory_type="observation", content="Project B uses Vue", project_path="/proj/b")
    memory_write_fact(memory_type="observation", content="Global note about React", project_path=None)

    results = memory_search("React", project_path="/proj/a")
    contents = [r["content"] for r in results["results"]]
    assert any("Project A" in c for c in contents)
    assert any("Global" in c for c in contents)
    assert not any("Project B" in c for c in contents)


def test_write_handoff_and_retrieve(temp_db):
    result = memory_write_handoff(
        session_id="sess-001",
        summary="Built the memory MCP server",
        decisions='["Used SQLite over Postgres", "FastMCP over raw SDK"]',
        next_steps='["Write tests", "Deploy to production"]',
    )
    assert result["id"]
    assert result["session_id"] == "sess-001"

    # Check status shows it
    status = memory_status()
    assert status["last_handoff"]["session_id"] == "sess-001"
    assert status["counts"]["handoffs"] == 1


def test_create_and_close_loop(temp_db):
    # Create
    loop = memory_create_loop(
        description="Finish dashboard layout",
        loop_type="task",
        priority="high",
    )
    assert loop["status"] == "open"
    assert loop["priority"] == "high"

    # Get open loops
    open_loops = memory_get_open_loops()
    assert open_loops["total_open"] == 1
    assert open_loops["loops"][0]["description"] == "Finish dashboard layout"

    # Close
    closed = memory_close_loop(loop["id"], "Completed in session 002")
    assert closed["status"] == "closed"
    assert closed["resolution"] == "Completed in session 002"

    # Verify closed loops excluded by default
    open_loops2 = memory_get_open_loops()
    assert open_loops2["total_open"] == 0


def test_get_open_loops_excludes_closed(temp_db):
    memory_create_loop(description="Open task", loop_type="task")
    loop2 = memory_create_loop(description="Will close", loop_type="task")
    memory_close_loop(loop2["id"], "Done")

    result = memory_get_open_loops()
    assert len(result["loops"]) == 1
    assert result["loops"][0]["description"] == "Open task"


def test_get_project_context(temp_db):
    proj = "/test/project"

    memory_write_fact(memory_type="project_context", content="Project uses FastAPI", project_path=proj)
    memory_write_fact(memory_type="preference", content="Global pref: use bun")
    memory_write_handoff(session_id="s1", summary="Set up project", project_path=proj)
    memory_create_loop(description="Add auth", loop_type="task", project_path=proj)

    ctx = memory_get_project_context(proj)

    assert ctx["project_path"] == proj
    assert ctx["continuity_brief"]["latest_handoff"]["summary"] == "Set up project"
    assert len(ctx["continuity_brief"]["open_loops"]) == 1
    assert len(ctx["continuity_brief"]["recent_memories"]) >= 1
    assert ctx["counts"]["project_memories"] == 1
    assert ctx["counts"]["project_loops"] == 1
    assert ctx["counts"]["project_handoffs"] == 1


def test_get_project_context_excludes_other_projects(temp_db):
    memory_write_fact(memory_type="project_context", content="Proj A fact", project_path="/a")
    memory_write_fact(memory_type="project_context", content="Proj B fact", project_path="/b")

    ctx = memory_get_project_context("/a", include_global=False)
    contents = [m["content"] for m in ctx["continuity_brief"]["recent_memories"]]
    assert "Proj A fact" in contents
    assert "Proj B fact" not in contents


def test_import_markdown_basic(temp_db, tmp_path):
    md = tmp_path / "test_memory.md"
    md.write_text(
        "# Project Memory\n\n"
        "## Preferences\n"
        "- Always use bun for JS work\n"
        "- Prefer dark theme\n\n"
        "## Corrections\n"
        "- robocopy exit code 1 means success, not failure\n",
        encoding="utf-8",
    )

    result = memory_import_markdown(str(md))
    assert result["entries_found"] == 3
    assert result["entries_imported"] == 3
    assert result["entries_skipped"] == 0


def test_import_markdown_idempotent(temp_db, tmp_path):
    md = tmp_path / "test_memory.md"
    md.write_text(
        "## Preferences\n- Use bun\n",
        encoding="utf-8",
    )

    memory_import_markdown(str(md))
    result2 = memory_import_markdown(str(md))
    assert result2["entries_imported"] == 0
    assert result2["entries_skipped"] == 1


def test_import_markdown_dry_run(temp_db, tmp_path):
    md = tmp_path / "test_memory.md"
    md.write_text(
        "## Preferences\n- Test entry\n",
        encoding="utf-8",
    )

    result = memory_import_markdown(str(md), dry_run=True)
    assert result["dry_run"] is True
    assert result["entries_found"] == 1
    assert result["entries_imported"] == 0

    # Verify nothing was written
    status = memory_status()
    assert status["counts"]["total_memories"] == 0


def test_read_codex_graceful_when_missing(temp_db, tmp_path, monkeypatch):
    """memory_read_codex should return a graceful error when cache is missing."""
    # Override _find_codex_cache_db to return None (simulate missing cache)
    monkeypatch.setattr("app.tools._find_codex_cache_db", lambda: None)

    result = memory_read_codex()
    # Should return a graceful error dict, not raise
    assert isinstance(result, dict)
    assert "error" in result
    assert result["total_matches"] == 0


def test_read_codex_returns_results_when_available(temp_db):
    """memory_read_codex should return results if Codex cache exists on this machine."""
    result = memory_read_codex()
    # On a machine with Codex, we should get results; otherwise graceful error
    assert isinstance(result, dict)
    if "error" not in result:
        assert "results" in result
        assert result["total_codex_events"] >= 0
