# Memory MCP Server

Shared-memory MCP server — canonical event store (OneDrive-backed) with local SQLite cache and cross-runtime bridge.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Claude Code  │     │   Cowork    │     │   Codex     │
│  (stdio)     │     │  (HTTP)     │     │  (bridge)   │
└──────┬───────┘     └──────┬──────┘     └──────┬──────┘
       │                    │                    │
       └────────┬───────────┘                    │
                │                                │
        ┌───────▼────────┐               ┌───────▼────────┐
        │  Memory MCP    │               │  Codex CLI     │
        │  Server        │               │  (writes own   │
        │  (this repo)   │               │   cache.sqlite)│
        └───────┬────────┘               └───────┬────────┘
                │                                │
       ┌────────┼─────────┐                      │
       │        │         │                      │
 ┌─────▼──┐ ┌──▼───┐ ┌───▼────┐          ┌──────▼──────┐
 │ SQLite  │ │Canon.│ │Outbox  │          │ cache.sqlite│
 │ Cache   │ │Store │ │(offline│          │ (Codex)     │
 │ (.db)   │ │(JSON)│ │writes) │          │             │
 └─────────┘ └──────┘ └────────┘          └─────────────┘
```

- **Canonical store** (source of truth): OneDrive-backed JSON event files at `~\OneDrive\.codex\memory\` — shared between Claude and Codex
- **SQLite cache**: Local `~/.memory-mcp/memory.db` — rebuilt from canonical events, not source of truth
- **Outbox**: Offline writes queued at `~/.memory-mcp/outbox/` when canonical store is unavailable
- **Codex bridge**: `memory_read_codex` reads Codex's `cache.sqlite` for cross-runtime memory sharing

## Features

- **14 MCP tools** for reading, writing, searching, and managing structured memories
- **Canonical event store** — append-only JSON events synced via OneDrive
- **SQLite + FTS5** cache for fast full-text search with ranking
- **Session handoffs** — structured records for cross-session continuity
- **Open loops** — track unfinished tasks, questions, blockers
- **Project scoping** — global and per-project memory separation
- **Secret rejection** — recursive scanning blocks API keys, tokens, passwords
- **Content-hash deduplication** — SHA-256 prevents aliasing distinct facts
- **Markdown import** — migrate existing MEMORY.md files
- **Codex bridge** — read Codex's memory cache for cross-environment sharing
- **Durable state** — memory_enabled persists across restarts
- **STDIO + HTTP** — works with Claude Code (stdio) and Cowork (HTTP)

## Quick Start

### Prerequisites
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

### Install

```powershell
cd C:\Users\set23\memory-mcp
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

Or manually:

```bash
cd ~/memory-mcp
uv sync --link-mode=copy
uv run python -c "from app.db import init_db; init_db()"
```

### Run (stdio — for Claude Code)

```bash
uv run --directory C:\Users\set23\memory-mcp python -m app
```

### Run (HTTP — for Cowork)

```bash
uv run --directory C:\Users\set23\memory-mcp python -m app --transport http --port 3097
```

## Configuration

### Claude Code (`.mcp.json` or `~/.claude.json`)

```json
{
  "mcpServers": {
    "memory-server": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:\\Users\\set23\\memory-mcp",
        "python",
        "-m",
        "app"
      ],
      "env": {}
    }
  }
}
```

### Cowork (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "memory-server": {
      "command": "cmd",
      "args": ["/c", "npx", "mcp-remote", "http://localhost:3097/mcp"]
    }
  }
}
```

## Tools

| Tool | Purpose |
|------|---------|
| `memory_status` | Health check, DB stats, version, canonical availability |
| `memory_read_recent` | Browse recent memories by type/project |
| `memory_search` | FTS5 full-text search with ranking |
| `memory_write_fact` | Store a new memory (with content-hash deduplication) |
| `memory_write_handoff` | Record session transition with decisions, open items, next steps |
| `memory_get_open_loops` | Get pending tasks/questions/blockers |
| `memory_create_loop` | Create a new open loop (UUID-based identity) |
| `memory_close_loop` | Close a loop with resolution |
| `memory_get_project_context` | Full continuity brief for a project |
| `memory_import_markdown` | Import from MEMORY.md files (idempotent) |
| `memory_read_codex` | Read Codex's memory cache (cross-runtime bridge) |
| `memory_set_enabled` | Enable/disable memory (durable across restarts) |
| `memory_get_enabled` | Check current enabled state |
| `memory_rebuild_cache` | Rebuild SQLite cache from canonical event store |

## Memory Types

| Type | Use For |
|------|---------|
| `preference` | User work preferences |
| `architecture_decision` | Design choices with rationale |
| `project_context` | Project-specific facts |
| `correction` | Mistakes and their corrections |
| `instruction` | Standing directives |
| `observation` | Learned patterns |
| `handoff` | Session transition records |

## Data Location

| Path | Content |
|------|---------|
| `~/.memory-mcp/memory.db` | SQLite cache (local, rebuilt from canonical) |
| `~/.memory-mcp/logs/` | Server logs |
| `~/.memory-mcp/backup/` | Daily backups |
| `~/.memory-mcp/outbox/` | Offline event queue |
| `~/.memory-mcp/memory_enabled.state` | Durable enabled/disabled state |
| `~/OneDrive/.codex/memory/` | Canonical event store (shared, source of truth) |

## Testing

```bash
cd ~/memory-mcp
uv run pytest tests/ -v
```

Tests use full monkeypatch isolation — all 4 modules (`app.config`, `app.db`, `app.tools`, `app.canonical`) are patched to use temp directories, preventing writes to the real canonical store.

## Security

- **Secret rejection**: Recursive scanning of all memory content for API keys, tokens, passwords, and secret-like patterns. Blocks writes containing secrets in any field including nested `details` payloads.
- **Append-only canonical store**: Events are never modified or deleted.
- **No network calls**: All I/O is local filesystem (OneDrive sync is OS-level, not application-level).

## Dependencies

- [FastMCP](https://github.com/jlowin/fastmcp) >= 2.8.0
- [Loguru](https://github.com/Delgan/loguru) >= 0.7.3
- [Pydantic](https://github.com/pydantic/pydantic) >= 2.0.0
- [Pydantic Settings](https://github.com/pydantic/pydantic-settings) >= 2.0.0

## License

MIT
