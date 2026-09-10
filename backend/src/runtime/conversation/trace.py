"""Export the canonical current-version trace of one conversation thread."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol

from backend.domain.runtime_state import RuntimeState
from backend.domain.turn_trace import TurnTrace


class ConversationTraceStore(Protocol):
    def load_nodes(self, session_id: str) -> Sequence[object]: ...

    def load_turn_trace(self, session_id: str, turn_id: str, data_idx: int) -> TurnTrace | None: ...


def conversation_trace_records(
    store: ConversationTraceStore, session_id: str, thread_id: str
) -> Iterator[dict[str, object]]:
    turns = sorted(
        (
            node
            for node in store.load_nodes(session_id)
            if isinstance(node, RuntimeState) and node.session_id == session_id and node.thread_id == thread_id
        ),
        key=lambda node: (node.timestamp, node.id),
    )
    for turn in turns:
        data_idx = turn.current_data_idx
        trace = store.load_turn_trace(session_id, turn.id, data_idx)
        identity = {
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn.id,
            "data_idx": data_idx,
        }
        yield {"type": "context", **identity, "data": trace.context.to_dict() if trace is not None else None}
        if trace is not None:
            for item in sorted(trace.items, key=lambda item: item.sequence):
                yield {"type": "item", **identity, "data": item.to_dict()}
