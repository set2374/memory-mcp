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

    # Runtime controls — loaded from state file if present
    memory_enabled: bool = True

    def _state_file(self) -> "Path":
        return self.db_path.parent / "memory_enabled.state"

    def load_enabled_state(self) -> None:
        """Read durable enabled/disabled state from disk."""
        sf = self._state_file()
        if sf.exists():
            try:
                self.memory_enabled = sf.read_text().strip().lower() == "true"
            except OSError:
                pass  # fallback to default

    def persist_enabled_state(self) -> None:
        """Write enabled/disabled state to disk so it survives restarts."""
        sf = self._state_file()
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text("true" if self.memory_enabled else "false")

    max_backup_days: int = 7
    log_rotation: str = "1 MB"
    log_retention: str = "1 week"

    model_config = {
        "env_prefix": "MEMORY_MCP_",
        "env_file": ".env",
        "extra": "ignore",
    }


config = Config()
config.load_enabled_state()
