"""Shared JSON-object primitives for local runtime persistence."""

from __future__ import annotations

import json
import sqlite3

from backend.domain.runtime_state import RuntimeNode, runtime_node_from_dict
from backend.domain.runtime_state.deltas import apply_turn_delta

from ..sqlite_json import read_json_object


class SQLiteJsonObjectMixin:
    def _touch_session(self, connection: sqlite3.Connection, session_id: str, timestamp: str) -> None:
        document = self._session_document(connection, session_id)
        document["updated_at"] = timestamp
        self._write_session_document(connection, session_id, document)

    @staticmethod
    def _objects(connection: sqlite3.Connection, session_id: str, namespace: str) -> list[RuntimeNode]:
        values = SQLiteJsonObjectMixin._json_values(connection, session_id, namespace)
        return [runtime_node_from_dict(value) for value in values]

    @staticmethod
    def _json_values(connection: sqlite3.Connection, session_id: str, namespace: str) -> list[dict[str, object]]:
        rows = connection.execute(
            "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace=?", (session_id, namespace)
        ).fetchall()
        values = [dict(value) for row in rows if isinstance(value := json.loads(str(row[0])), dict)]
        if namespace == "runtime_node":
            for value in values:
                SQLiteJsonObjectMixin._merge_turn_deltas(connection, session_id, value)
        return values

    @staticmethod
    def _put_json_object(
        connection: sqlite3.Connection,
        session_id: str,
        namespace: str,
        object_id: str,
        payload: dict[str, object],
        updated_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (
                session_id,
                namespace,
                object_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                updated_at,
            ),
        )
        if namespace == "runtime_node":
            connection.execute(
                "DELETE FROM json_objects WHERE session_id=? AND namespace=?",
                (session_id, f"runtime_delta:{object_id}"),
            )

    @staticmethod
    def _json_object(
        connection: sqlite3.Connection, session_id: str, namespace: str, object_id: str
    ) -> dict[str, object] | None:
        value = read_json_object(connection, session_id, namespace, object_id)
        if namespace == "runtime_node" and value is not None:
            SQLiteJsonObjectMixin._merge_turn_deltas(connection, session_id, value)
        return value

    @staticmethod
    def _merge_turn_deltas(connection: sqlite3.Connection, session_id: str, payload: dict[str, object]) -> None:
        rows = connection.execute(
            "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace=? ORDER BY object_id",
            (session_id, f"runtime_delta:{payload['id']}"),
        )
        for row in rows:
            apply_turn_delta(payload, json.loads(row[0])["frame"])

    @staticmethod
    def _assert_writable(_connection: sqlite3.Connection) -> None:
        """All v12 sessions are local and writable after lifecycle checks."""
