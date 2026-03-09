"""Tests for database schema and initialization."""

from app.db import get_connection, init_db, db_size_kb


def test_init_db_creates_tables(temp_db):
    conn = get_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [t["name"] for t in tables]
    conn.close()

    assert "memories" in names
    assert "open_loops" in names
    assert "handoffs" in names
    assert "memories_fts" in names


def test_init_db_idempotent(temp_db):
    """Running init_db twice should not raise."""
    init_db()
    init_db()

    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    conn.close()
    assert count == 0


def test_wal_mode_enabled(temp_db):
    conn = get_connection()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_foreign_keys_enabled(temp_db):
    conn = get_connection()
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.close()
    assert fk == 1


def test_memory_type_check_constraint(temp_db):
    """Invalid memory_type should be rejected."""
    conn = get_connection()
    import sqlite3
    try:
        conn.execute(
            "INSERT INTO memories (id, memory_type, content) VALUES ('test1', 'invalid_type', 'test')"
        )
        conn.commit()
        assert False, "Should have raised"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


def test_valid_memory_types(temp_db):
    """All valid memory types should work."""
    conn = get_connection()
    valid_types = [
        "preference", "architecture_decision", "project_context",
        "correction", "instruction", "observation", "handoff",
    ]
    for i, mt in enumerate(valid_types):
        conn.execute(
            "INSERT INTO memories (id, memory_type, content) VALUES (?, ?, ?)",
            (f"test_{i}", mt, f"content for {mt}"),
        )
    conn.commit()

    count = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    conn.close()
    assert count == len(valid_types)


def test_fts_sync_on_insert(temp_db):
    """FTS table should be populated on insert."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO memories (id, memory_type, content) VALUES ('fts1', 'observation', 'unique_test_keyword_xyz')"
    )
    conn.commit()

    results = conn.execute(
        "SELECT * FROM memories_fts WHERE memories_fts MATCH 'unique_test_keyword_xyz'"
    ).fetchall()
    conn.close()
    assert len(results) == 1


def test_db_size_kb(temp_db):
    size = db_size_kb()
    assert size > 0
