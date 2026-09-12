"""SQLite connection and common persistence lifecycle helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from threading import local

from backend.configuration import ClientPaths

from .sqlite_schema import SCHEMA


class ChangeConnection(sqlite3.Connection):
    """Track changed domains without parsing SQL or serializing message payloads."""

    changed_namespaces: set[str]


class SQLiteBaseMixin:
    def session_ids(self) -> Iterator[str]:
        """Locate databases without reading or reconstructing conversation histories."""
        for directory in self.paths.runtime_dir.iterdir():
            database = directory / "state.db"
            if (
                directory.is_dir()
                and not directory.is_symlink()
                and not database.is_symlink()
                and database.is_file()
                and database.stat().st_size
            ):
                yield directory.name

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
        self,
        session_id: str,
        *,
        initialize: bool = False,
        refresh_index: bool = True,
        write: bool = False,
        notify: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        path = self.paths.session_db(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        cached = getattr(self._stream_local, "connections", None)
        connection = cached.get(session_id) if cached is not None else None
        fresh = connection is None
        if fresh:
            connection = sqlite3.connect(path, factory=ChangeConnection)
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
            connection.changed_namespaces = set()
            yield connection
            changed = connection.total_changes > baseline_changes
            namespaces = connection.changed_namespaces
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
        if committed and changed and notify and cached is None and namespaces:
            callback = getattr(self.agent_thread_index, "on_store_change", None)
            if callable(callback):
                if namespaces & {"session", "sidebar_thread", "runtime_node"}:
                    callback(session_id, "session.changed")
                elif namespaces & {"right_panel_state", "right_panel_window"}:
                    callback(session_id, "panel.changed")
