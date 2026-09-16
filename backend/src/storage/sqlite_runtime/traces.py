"""Aggregated Turn Trace persistence on the Session JSON-object store."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from backend.domain.runtime_state import RuntimeState
from backend.domain.turn_trace import TurnTrace, TurnTraceContext, TurnTraceItem


class SQLiteTurnTraceMixin:
    _TRACE_NAMESPACE = "turn_trace"

    @staticmethod
    def _validate_turn(trace: TurnTrace, turn: RuntimeState) -> None:
        if trace.data_idx < 0 or trace.data_idx >= len(turn.data):
            raise ValueError("Trace data_idx is out of range.")
        if turn.id != trace.turn_id or turn.thread_id != trace.thread_id:
            raise ValueError("Trace identity does not match its Turn.")

    def initialize_turn_trace(self, session_id: str, trace: TurnTrace) -> TurnTrace:
        """Create the immutable context once and return the stored aggregate."""

        with self._connection_for_existing(session_id, write=True) as connection:
            self._assert_writable(connection)
            node = self._json_object(connection, session_id, "runtime_node", trace.turn_id)
            if node is None:
                raise ValueError(f"Unknown Turn: {trace.turn_id}")
            turn = RuntimeState.from_dict(node)
            self._validate_turn(trace, turn)
            payload = self._json_object(connection, session_id, self._TRACE_NAMESPACE, trace.object_id)
            if payload is not None:
                stored = TurnTrace.from_dict(payload)
                self._validate_turn(stored, turn)
                if stored.context.initialized_at:
                    return stored
                trace = replace(
                    stored,
                    context=trace.context,
                    items=[
                        *stored.items,
                        *(
                            replace(item, sequence=stored.last_sequence + index + 1)
                            for index, item in enumerate(trace.items)
                        ),
                    ],
                    last_sequence=stored.last_sequence + len(trace.items),
                    updated_at=trace.updated_at,
                )
            self._put_json_object(
                connection,
                session_id,
                self._TRACE_NAMESPACE,
                trace.object_id,
                trace.to_dict(),
                trace.updated_at,
            )
            return trace

    def append_turn_trace_item(
        self,
        session_id: str,
        turn_id: str,
        data_idx: int,
        *,
        message_idx: int | None,
        item_idx: int | None,
        role: str,
        item: dict[str, Any],
        completed_at: str,
    ) -> TurnTrace | None:
        """Append an audit record; message positions never determine identity."""

        object_id = f"{turn_id}:{data_idx}"
        with self._connection_for_existing(session_id, write=True) as connection:
            self._assert_writable(connection)
            payload = self._json_object(connection, session_id, self._TRACE_NAMESPACE, object_id)
            node = self._json_object(connection, session_id, "runtime_node", turn_id)
            if node is None:
                raise ValueError(f"Unknown Turn: {turn_id}")
            turn = RuntimeState.from_dict(node)
            is_event = role == "runtime" and item.get("type") == "runtime_event"
            if payload is None:
                if not is_event:
                    return None
                trace = TurnTrace(
                    turn_id=turn_id,
                    thread_id=turn.thread_id,
                    data_idx=data_idx,
                    context=TurnTraceContext("", [], [], ""),
                    items=[],
                    last_sequence=0,
                    updated_at=completed_at,
                )
            else:
                trace = TurnTrace.from_dict(payload)
            self._validate_turn(trace, turn)
            if is_event:
                if message_idx is not None or item_idx is not None:
                    raise ValueError("Runtime events do not have message positions.")
            else:
                if message_idx is None or not 0 <= message_idx < len(turn.data[data_idx]):
                    raise ValueError("Trace message_idx is out of range.")
                message = turn.data[data_idx][message_idx]
                content = message.get("content", [])
                if item_idx is None or not 0 <= item_idx < len(content):
                    raise ValueError("Trace item_idx is out of range.")
                if message.get("role") != role:
                    raise ValueError("Trace Item role does not match its Turn.")
                if content[item_idx].get("status") not in {"success", "failed"}:
                    raise ValueError("Only terminal Turn Items can be traced.")
                if item.get("status") not in {"success", "failed"}:
                    raise ValueError("Only terminal audit Items can be persisted.")

            sequence = trace.last_sequence + 1
            entry = TurnTraceItem(
                sequence=sequence,
                message_idx=message_idx,
                item_idx=item_idx,
                role=role,
                item=item,
                completed_at=completed_at,
            )
            updated = replace(
                trace,
                items=[*trace.items, entry],
                last_sequence=sequence,
                updated_at=completed_at,
            )
            self._put_json_object(
                connection,
                session_id,
                self._TRACE_NAMESPACE,
                object_id,
                updated.to_dict(),
                completed_at,
            )
            return updated

    def load_turn_trace(
        self,
        session_id: str,
        turn_id: str,
        data_idx: int,
        *,
        after_sequence: int | None = None,
    ) -> TurnTrace | None:
        if data_idx < 0:
            raise ValueError("Trace data_idx must be non-negative.")
        if after_sequence is not None and after_sequence < 0:
            raise ValueError("after_sequence must be non-negative.")
        object_id = f"{turn_id}:{data_idx}"
        with self._connection_for_existing(session_id) as connection:
            payload = self._json_object(connection, session_id, self._TRACE_NAMESPACE, object_id)
        if payload is None:
            return None
        trace = TurnTrace.from_dict(payload)
        if after_sequence is None:
            return trace
        return replace(trace, items=[item for item in trace.items if item.sequence > after_sequence])

    def load_thread_trace(
        self,
        session_id: str,
        thread_id: str,
        turn_id: str,
        data_idx: int,
        *,
        after_sequence: int | None = None,
    ) -> TurnTrace | None:
        """Read one Trace directly without loading or validating its Turn."""

        path = self.paths.session_db(session_id)
        if not path.is_file():
            return None
        object_id = f"{turn_id}:{data_idx}"
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT payload_json FROM json_objects "
                "WHERE session_id=? AND namespace=? AND object_id=? "
                "AND json_extract(payload_json,'$.thread_id')=?",
                (session_id, self._TRACE_NAMESPACE, object_id, thread_id),
            ).fetchone()
        if row is None:
            return None
        trace = TurnTrace.from_dict(json.loads(str(row[0])))
        if after_sequence is None:
            return trace
        return replace(trace, items=[item for item in trace.items if item.sequence > after_sequence])


__all__ = ["SQLiteTurnTraceMixin"]
