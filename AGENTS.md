# MEMORY MCP SERVER — USAGE POLICY

## Purpose

This MCP server provides persistent, structured memory for Claude Code (and Cowork)
sessions. It stores cross-session context in a local SQLite database so that each
new session can pick up where the last one left off.

**Design principle:** Favor the appearance of continuity over large-volume storage.
Store concise, structured facts — not noisy ephemeral notes.

## When to Use Memory Tools

### Session Start (every session)
1. Call `memory_status` to verify the server is available
2. Call `memory_get_project_context(project_path=<cwd>)` to get the continuity brief
3. Review the brief **silently** — do NOT dump it to the user unless asked
4. If there's a recent handoff or open loops, briefly mention what was left pending

### During Session
| Trigger | Action |
|---------|--------|
| User says "remember" | `memory_write_fact` with appropriate type |
| User corrects a mistake | `memory_write_fact(memory_type="correction")` |
| Preference revealed | `memory_write_fact(memory_type="preference")` |
| Architecture/design decision made | `memory_write_fact(memory_type="architecture_decision")` |
| Work deferred for later | `memory_create_loop(loop_type="task")` |
| Question left unresolved | `memory_create_loop(loop_type="question")` |
| Waiting on external input | `memory_create_loop(loop_type="blocker")` |
| A pending loop is completed | `memory_close_loop(loop_id, resolution)` |
| Completing a multi-step task | Consider a mid-session handoff or loop updates |

### Session End
When the session is wrapping up (user says "done", "wrap up", "that's all",
or the conversation is naturally concluding):
1. `memory_write_handoff` — session summary, decisions, open items, next steps
2. `memory_write_fact` — any new cross-session learnings
3. `memory_create_loop` — any new unresolved items

### Search
- `memory_search(query)` — when you need to recall something specific
- `memory_read_recent(memory_type=...)` — browse recent entries by category
- `memory_get_open_loops` — check what's pending

## What to Store

| Memory Type | Store When | Examples |
|---|---|---|
| `preference` | User reveals a work preference | "Use bun not npm", "Direct style" |
| `architecture_decision` | A design choice is made with rationale | "SQLite over Postgres for local-only" |
| `project_context` | Project-specific facts established | "Client: Acme Corp, Case No: 12345" |
| `correction` | User corrects an error | "robocopy exit 1 = success, not failure" |
| `instruction` | Standing directive given | "Always check CourtListener before Midpage" |
| `observation` | Learned pattern confirmed | "PowerShell $_ vars consumed by bash" |
| `handoff` | End of session | Session summary, next steps |

## What NOT to Store
- Temporary context that only matters for the current session
- Anything already in CLAUDE.md (framework instructions auto-load every session)
- Anything already in checkpoint.md (ALP-managed matter state)
- Anything containing secrets, passwords, or API keys
- Raw conversation content or user messages verbatim
- Speculative or unverified conclusions from reading a single file

## Relationship to ALP Framework

This memory server is **complementary** to the ALP framework — not competing:

| System | Manages | Scope |
|--------|---------|-------|
| Memory MCP | Cross-session recall, handoffs, preferences, open loops | ALL projects |
| MEMORY.md (auto-loaded) | Claude Code per-project session memory | Per-project |
| checkpoint.md | Legal matter state, procedural posture | ALP legal work only |
| timelog.md | Billing time entries | ALP legal work only |
| Persistent Memory .md | Legacy cross-project memory (bridge) | Cross-project |

When ALP is active (legal work), use BOTH systems:
- Memory MCP for session handoffs and cross-session learnings
- ALP checkpoint/timelog for matter-specific state

When ALP is dormant (non-legal work like coding, games, dashboards):
- Memory MCP is the primary continuity mechanism

## Scoping Rules

- `project_path = null` → **global** memory (applies everywhere)
- `project_path = "<path>"` → **project-scoped** (returned for that project + global)
- Use project paths as they appear in the working directory
- Global preferences apply to all projects automatically

## Fallback Behavior

If the memory server is unavailable (`required: false`):
1. Fall back to reading MEMORY.md files directly via Read tool
2. Store notes in project MEMORY.md via Edit tool
3. **Do not block work** due to memory unavailability
4. Mention once: "Memory server unavailable — using markdown fallback"

## Daily Operator Prompt

> At the start of every session, silently call `memory_get_project_context`
> with the current working directory. Review the continuity brief internally.
> If there's a recent handoff or open loops, briefly mention what was left
> pending. Before ending a session, call `memory_write_handoff` with a
> summary of what was accomplished, decisions made, and recommended next steps.
