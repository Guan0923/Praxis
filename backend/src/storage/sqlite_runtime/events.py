"""Persistent Runtime frame sequence and canonical deltas."""

from __future__ import annotations

import json
import sqlite3

from backend.domain.runtime_state import NodeFrame, RuntimeState, runtime_node_from_dict, utc_iso


class SQLiteRuntimeEventMixin:
    def _advance_frame_sequence(self, connection: sqlite3.Connection, frame: NodeFrame) -> int:
        state_row = connection.execute(
            "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace='runtime_event_state' AND object_id=?",
            (frame.session_id, frame.turn_id),
        ).fetchone()
        last_sequence = 0
        if state_row is not None:
            value = json.loads(str(state_row[0]))
            if isinstance(value, dict):
                last_sequence = int(value.get("last_sequence") or 0)
        sequence = last_sequence + 1
        if frame.sequence and frame.sequence != sequence:
            raise ValueError("Runtime persistence sequence is out of order.")
        self._put_json_object(
            connection,
            frame.session_id,
            "runtime_event_state",
            frame.turn_id,
            {"last_sequence": sequence},
            utc_iso(),
        )
        return sequence

    def append_runtime_delta(self, frame: NodeFrame, *, thread_id: str, status: str) -> None:
        if frame.type != "turn.delta":
            raise ValueError("An incremental write requires a Turn delta.")
        activity = "status" in frame.patch or any(op.get("op") == "append_message" for op in frame.operations)
        with self._connection(frame.session_id, refresh_index=activity, write=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM json_objects WHERE session_id=? AND namespace='runtime_node' AND object_id=?",
                (frame.session_id, frame.turn_id),
            ).fetchone()
            if exists is None:
                raise KeyError(frame.turn_id)
            sequence = self._advance_frame_sequence(connection, frame)
            self._put_json_object(
                connection,
                frame.session_id,
                f"runtime_delta:{frame.turn_id}",
                f"{sequence:020d}",
                {"event_id": frame.event_id, "sequence": sequence, "frame": frame.to_dict()},
                utc_iso(),
            )
            if activity:
                self._set_thread_head(
                    connection,
                    session_id=frame.session_id,
                    thread_id=thread_id,
                    turn_id=frame.turn_id,
                    timestamp=utc_iso(),
                    clear_running=status != "running",
                )
                self._touch_session(connection, frame.session_id, utc_iso())

    def runtime_event_sequence(self, session_id: str, turn_id: str) -> int:
        with self._connection(session_id) as connection:
            state = self._json_object(connection, session_id, "runtime_event_state", turn_id) or {}
            return int(state.get("last_sequence") or 0)

    def runtime_stream_snapshot(self, session_id: str, turn_id: str) -> tuple[RuntimeState | None, int]:
        if not self.paths.session_db(session_id).exists():
            return None, 0
        with self._connection(session_id) as connection:
            node_payload = self._json_object(connection, session_id, "runtime_node", turn_id)
            state = self._json_object(connection, session_id, "runtime_event_state", turn_id) or {}
        if node_payload is None:
            return None, int(state.get("last_sequence") or 0)
        node = runtime_node_from_dict(node_payload)
        return (node if isinstance(node, RuntimeState) else None), int(state.get("last_sequence") or 0)


__all__ = ["SQLiteRuntimeEventMixin"]
