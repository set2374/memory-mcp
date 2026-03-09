# install.ps1 — Memory MCP Server installer
# Run: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$ServerDir = "$env:USERPROFILE\memory-mcp"
$DataDir = "$env:USERPROFILE\.memory-mcp"
$StartupDir = [Environment]::GetFolderPath('Startup')

Write-Host "=== Memory MCP Server Installer ===" -ForegroundColor Cyan

# 1. Verify prerequisites
Write-Host "`n[1/5] Checking prerequisites..."
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Error "uv not found. Install from https://docs.astral.sh/uv/"
    exit 1
}
Write-Host "  uv: $(uv --version)"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Error "Python not found."
    exit 1
}
Write-Host "  Python: $(python --version)"

# 2. Create data directories
Write-Host "[2/5] Creating data directories..."
New-Item -ItemType Directory -Force -Path "$DataDir\logs" | Out-Null
New-Item -ItemType Directory -Force -Path "$DataDir\backup" | Out-Null
Write-Host "  Data: $DataDir"

# 3. Install dependencies
Write-Host "[3/5] Installing Python dependencies via uv..."
Push-Location $ServerDir
uv sync
Pop-Location

# 4. Initialize database
Write-Host "[4/5] Initializing database..."
Push-Location $ServerDir
uv run python -c "from app.db import init_db; init_db()"
Pop-Location
Write-Host "  Database: $DataDir\memory.db"

# 5. Install Cowork HTTP startup script
Write-Host "[5/5] Installing startup script for Cowork HTTP transport..."
Copy-Item "$ServerDir\scripts\start-memory-mcp.bat" "$StartupDir\start-memory-mcp.bat" -Force
Write-Host "  Startup: $StartupDir\start-memory-mcp.bat"

Write-Host "`n=== Installation Complete ===" -ForegroundColor Green
Write-Host "  Server code:  $ServerDir"
Write-Host "  Database:     $DataDir\memory.db"
Write-Host "  Logs:         $DataDir\logs\"
Write-Host "  HTTP port:    3097 (for Cowork)"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Add memory-server entry to ~/.claude.json or .mcp.json"
Write-Host "  2. Restart Claude Code to discover the new MCP server"
Write-Host "  3. Toggle on with @memory-server in Claude Code"
Write-Host ""
Write-Host "See AGENTS.md and README.md for usage policy and configuration."
