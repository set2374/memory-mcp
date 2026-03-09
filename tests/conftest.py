"""Test fixtures for Memory MCP Server."""

import os
import tempfile
from pathlib import Path

import pytest

from app.config import Config


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Use a temporary database for every test."""
    db_path = tmp_path / "test_memory.db"
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backup"
    log_dir.mkdir()
    backup_dir.mkdir()

    # Patch the config module's singleton
    monkeypatch.setattr("app.config.config", Config(
        db_path=db_path,
        log_dir=log_dir,
        backup_dir=backup_dir,
    ))

    # Also patch via tools and db modules that import config
    monkeypatch.setattr("app.db.config", Config(
        db_path=db_path,
        log_dir=log_dir,
        backup_dir=backup_dir,
    ))

    from app.db import init_db
    init_db()

    return db_path
