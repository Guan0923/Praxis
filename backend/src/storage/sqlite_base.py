"""SQLite connection and common persistence lifecycle helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from threading import local

from backend.configuration import ClientPaths

from .sqlite_schema import SCHEMA


class SQLiteBaseMixin:
    def __init__(self, paths: ClientPaths, agent_thread_index: object | None = None) -> None:
        self.paths = paths
        self.agent_thread_index = agent_thread_index
        self.paths.ensure()
        self._stream_local = local()

    @contextmanager
    def streaming_connections(self):
        """Reuse connections only inside the dedicated, ordered persistence worker."""
        self._stream_local.connections = {}
        try:
            yield
        finally:
            for connection in self._stream_local.connections.values():
                connection.close()
            del self._stream_local.connections

    @contextmanager
    def _connection(
        self, session_id: str, *, initialize: bool = False, refresh_index: bool = True, write: bool = False
    ) -> Iterator[sqlite3.Connection]:
        path = self.paths.session_db(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        cached = getattr(self._stream_local, "connections", None)
        connection = cached.get(session_id) if cached is not None else None
        fresh = connection is None
        if fresh:
            connection = sqlite3.connect(path)
        committed = False
        changed = False
        try:
            if fresh:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                self._assert_supported_schema(connection)
                self._prepare_schema(connection)
                connection.executescript(("BEGIN IMMEDIATE;\n" if initialize else "") + SCHEMA)
                self._validate_schema(connection)
                if cached is not None:
                    cached[session_id] = connection
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            baseline_changes = connection.total_changes
            yield connection
            changed = connection.total_changes > baseline_changes
            connection.commit()
            committed = True
        except Exception:
            connection.rollback()
            raise
        finally:
            if cached is None or session_id not in cached:
                connection.close()
        if committed and changed and refresh_index and self.agent_thread_index is not None:
            refresh = getattr(self.agent_thread_index, "refresh_session", None)
            if callable(refresh):
                refresh(self, session_id)
