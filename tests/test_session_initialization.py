from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.configuration import ClientPaths
from backend.storage import SQLiteSessionStore, sqlite_sessions


def test_scanning_prepared_workspace_does_not_initialize_an_incomplete_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ClientPaths(tmp_path)
    store = SQLiteSessionStore(paths)
    session_id = "session-initializing"
    paths.ensure_session(session_id)

    assert store.list_sessions() == []
    assert store.list_sidebar_thread_summaries() == []
    monkeypatch.setattr(sqlite_sessions, "new_session_id", lambda: session_id)
    session = store.create_session("ready")

    assert store.get_session(session_id) == session


def test_new_session_schema_is_not_published_before_its_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ClientPaths(tmp_path)
    session_id = "session-atomic"
    monkeypatch.setattr(sqlite_sessions, "new_session_id", lambda: session_id)
    observed_tables: list[tuple[str]] = []

    class ObservedStore(SQLiteSessionStore):
        def _validate_schema(self, connection: sqlite3.Connection) -> None:
            super()._validate_schema(connection)
            with sqlite3.connect(paths.session_db(session_id)) as reader:
                observed_tables.extend(reader.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())

    ObservedStore(paths).create_session("ready")

    assert observed_tables == []
    assert SQLiteSessionStore(paths).get_session(session_id).title == "ready"
