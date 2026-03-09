# Memory MCP Server

Local-only MCP memory server for Claude Code — SQLite-backed persistent memory across sessions.

## Features

- **10 MCP tools** for reading, writing, and searching structured memories
- **SQLite + FTS5** for fast full-text search with ranking
- **Session handoffs** — structured records for cross-session continuity
- **Open loops** — track unfinished tasks, questions, blockers
- **Project scoping** — global and per-project memory separation
- **Markdown import** — migrate existing MEMORY.md files
- **No network calls** — all data stays on your machine
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
uv sync
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
| `memory_status` | Health check, DB stats |
| `memory_read_recent` | Browse recent memories by type/project |
| `memory_search` | FTS5 full-text search |
| `memory_write_fact` | Store a new memory |
| `memory_write_handoff` | Record session transition |
| `memory_get_open_loops` | Get pending tasks/questions |
| `memory_create_loop` | Create a new open loop |
| `memory_close_loop` | Close a loop with resolution |
| `memory_get_project_context` | Full continuity brief |
| `memory_import_markdown` | Import from MEMORY.md |

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
| `~/.memory-mcp/memory.db` | SQLite database |
| `~/.memory-mcp/logs/` | Server logs |
| `~/.memory-mcp/backup/` | Daily backups |

## Testing

```bash
cd ~/memory-mcp
uv run pytest tests/ -v
```

## Architecture

- **Framework:** FastMCP (Python)
- **Storage:** SQLite with WAL mode + FTS5 full-text search
- **Transport:** stdio (Claude Code) / HTTP (Cowork on port 3097)
- **Dependencies:** fastmcp, loguru, pydantic, pydantic-settings
- **No network calls** — purely local I/O

## License

MIT
