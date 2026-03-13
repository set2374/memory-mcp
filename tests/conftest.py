"""Test fixtures for Memory MCP Server."""

import os
import tempfile
from pathlib import Path

import pytest

from app.config import Config


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Use a temporary database AND temporary canonical store for every test.

    Patches config in ALL modules that import it at module level:
    app.config, app.db, app.tools, app.canonical.
    Without this, writes go to the real OneDrive-backed canonical store.
    """
    db_path = tmp_path / "test_memory.db"
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backup"
    canonical_root = tmp_path / "canonical"
    outbox_dir = tmp_path / "outbox"
    log_dir.mkdir()
    backup_dir.mkdir()
    canonical_root.mkdir()
    outbox_dir.mkdir()

    test_config = Config(
        db_path=db_path,
        log_dir=log_dir,
        backup_dir=backup_dir,
        canonical_root=canonical_root,
        outbox_dir=outbox_dir,
    )

    # Patch config in EVERY module that imports it at load time
    monkeypatch.setattr("app.config.config", test_config)
    monkeypatch.setattr("app.db.config", test_config)
    monkeypatch.setattr("app.tools.config", test_config)
    monkeypatch.setattr("app.canonical.config", test_config)

    from app.db import init_db
    init_db()

    return db_path
