"""Configuration for Memory MCP Server."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Config(BaseSettings):
    """Server configuration with environment variable overrides."""

    host: str = "127.0.0.1"
    port: int = 3097
    transport: str = "stdio"

    db_path: Path = Path.home() / ".memory-mcp" / "memory.db"
    log_dir: Path = Path.home() / ".memory-mcp" / "logs"
    backup_dir: Path = Path.home() / ".memory-mcp" / "backup"

    max_backup_days: int = 7
    log_rotation: str = "1 MB"
    log_retention: str = "1 week"

    model_config = {
        "env_prefix": "MEMORY_MCP_",
        "env_file": ".env",
        "extra": "ignore",
    }


config = Config()
