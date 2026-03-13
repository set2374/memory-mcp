"""Configuration for Memory MCP Server."""

import platform
from pathlib import Path

from pydantic_settings import BaseSettings


def _default_canonical_root() -> Path:
    """Default canonical shared-memory root (same as Codex)."""
    return Path.home() / "OneDrive - turmanlegal.com" / ".codex" / "memory"


def _default_machine_id() -> str:
    """Stable machine identifier derived from hostname."""
    return platform.node().lower().replace(" ", "-")


class Config(BaseSettings):
    """Server configuration with environment variable overrides."""

    host: str = "127.0.0.1"
    port: int = 3097
    transport: str = "stdio"

    # Local cache (SQLite) — NOT source of truth
    db_path: Path = Path.home() / ".memory-mcp" / "memory.db"
    log_dir: Path = Path.home() / ".memory-mcp" / "logs"
    backup_dir: Path = Path.home() / ".memory-mcp" / "backup"

    # Canonical shared event store (OneDrive-backed, shared with Codex)
    canonical_root: Path = _default_canonical_root()
    machine_id: str = _default_machine_id()

    # Local outbox for offline/degraded writes
    outbox_dir: Path = Path.home() / ".memory-mcp" / "outbox"

    # Runtime controls
    memory_enabled: bool = True

    max_backup_days: int = 7
    log_rotation: str = "1 MB"
    log_retention: str = "1 week"

    model_config = {
        "env_prefix": "MEMORY_MCP_",
        "env_file": ".env",
        "extra": "ignore",
    }


config = Config()
