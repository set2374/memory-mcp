@echo off
REM Memory MCP Server — HTTP Transport for Cowork
REM Port 3097 — runs in background for Claude Desktop access

set SERVER_DIR=C:\Users\set23\memory-mcp
set PORT=3097

if "%1"=="stop" (
    for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
        taskkill /PID %%p /F >nul 2>&1
    )
    echo Memory MCP server stopped.
    exit /b 0
)

REM Check if already running
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo Memory MCP already running on port %PORT%
    exit /b 0
)

echo Starting Memory MCP server on http://localhost:%PORT%/mcp
cd /d "%SERVER_DIR%"
start /min "Memory MCP Server" uv run python -m app --transport http --port %PORT%
timeout /t 2 /nobreak >nul
echo Memory MCP server started.
