from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.runtime_event_transport import (
    publish_frame,
    publish_terminal,
    thread_sse,
    turn_sse,
)
from backend.configuration import ClientPaths
from backend.domain.runtime_state import NodeFrame, RuntimeState, utc_iso
from backend.storage import runtime_event_stream as event_stream_module
from backend.storage.runtime_event_stream import (
    MemoryRuntimeEventStream,
    RuntimeEventCursorExpired,
)
from backend.storage.sqlite import SQLiteSessionStore


@pytest.fixture
def event_stream() -> MemoryRuntimeEventStream:
    stream = MemoryRuntimeEventStream()
    yield stream
    stream.close()


def _canonical_turn(tmp_path: Path) -> tuple[ClientPaths, SQLiteSessionStore, RuntimeState, NodeFrame]:
    paths = ClientPaths(tmp_path / "data")
    paths.ensure()
    store = SQLiteSessionStore(paths)
    session = store.create_session("events")
    store.create_sidebar_thread(
        session_id=session.session_id,
        thread_id=session.session_id,
        title="events",
    )
    root = store.ensure_root_node(session.session_id, id="turn-root")
    node = RuntimeState.create(
        session_id=session.session_id,
        thread_id=session.session_id,
        id="turn-event",
        parent=root,
        user_content=[{"type": "text", "text": "hello", "status": "success"}],
    )
    frame = NodeFrame.snapshot(node)
    store.create_node_with_frame(node, frame)
    return paths, store, node, frame


def _empty_turn_state(tmp_path: Path, stream) -> tuple[SimpleNamespace, str, str]:
    paths = ClientPaths(tmp_path / "data")
    paths.ensure()
    store = SQLiteSessionStore(paths)
    session = store.create_session("events")
    store.create_sidebar_thread(
        session_id=session.session_id,
        thread_id=session.session_id,
        title="events",
    )
    return (
        SimpleNamespace(
            paths=paths,
            agent_thread_index=None,
            runtime_event_stream=stream,
            active_turn_streams={},
        ),
        session.session_id,
        session.session_id,
    )


def test_memory_publication_is_idempotent_without_persistent_outbox(tmp_path: Path, event_stream):
    stream = event_stream
    paths, store, node, frame = _canonical_turn(tmp_path)
    state = SimpleNamespace(paths=paths, agent_thread_index=None, runtime_event_stream=stream)
    publish_frame(state, frame, node)
    publish_frame(state, frame, node)
    assert len(stream.read_turn(node.id, "0", block_ms=0)) == 1
    for _ in range(2):
        publish_terminal(
            state, session_id=node.session_id, thread_id=node.thread_id, turn_id=node.id, terminal_type="success"
        )
    assert len(stream.read_thread(node.thread_id, "0", block_ms=0)) == 2
    with store._connection(node.session_id) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM json_objects WHERE namespace='runtime_event_outbox'").fetchone()[0]
            == 0
        )


def test_event_stream_enforces_exact_maxlen(monkeypatch, event_stream):
    monkeypatch.setattr(event_stream_module, "EVENT_STREAM_MAXLEN", 3)
    for sequence in range(1, 6):
        event_stream.publish(
            event_id=str(sequence),
            turn_id="turn",
            thread_id="thread",
            sequence=sequence,
            payload={"type": "turn.delta"},
        )
    assert [e.sequence for e in event_stream.read_turn("turn", "0", block_ms=0)] == [3, 4, 5]
    with pytest.raises(RuntimeEventCursorExpired):
        event_stream.read_thread("thread", "0", block_ms=0)
    assert [e.sequence for e in event_stream.read_thread("thread", "2", block_ms=0)] == [3, 4, 5]


def test_turn_sse_replays_terminal_published_before_sqlite_baseline(
    tmp_path: Path,
    event_stream: MemoryRuntimeEventStream,
) -> None:
    stream = event_stream
    state, session_id, thread_id = _empty_turn_state(tmp_path, stream)
    turn_id = "turn-before-baseline"
    publish_terminal(
        state,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        terminal_type="failed",
        message="startup failed",
    )

    async def receive() -> list[str]:
        return [item async for item in turn_sse(state, session_id, thread_id, turn_id)]

    events = asyncio.run(receive())
    assert len(events) == 1
    assert events[0].startswith(f"id: {state.runtime_event_epoch}|{stream.latest_thread_id(thread_id)}\ndata: <SSE")
    assert f'id="{turn_id}" type="failed">startup failed</SSE>' in events[0]


def test_turn_sse_catches_terminal_published_during_initial_catch_up(tmp_path: Path) -> None:
    turn_id = "turn-during-catch-up"

    class PublishingStream(MemoryRuntimeEventStream):
        def __init__(self) -> None:
            super().__init__()
            self.session_id = ""
            self.thread_id = ""
            self.published = False

        def latest_turn_event(self, candidate_turn_id: str):
            if candidate_turn_id == turn_id and not self.published:
                self.published = True
                self.publish(
                    event_id="terminal-during-catch-up",
                    turn_id=turn_id,
                    thread_id=self.thread_id,
                    sequence=1,
                    payload={
                        "type": "turn.terminal",
                        "session_id": self.session_id,
                        "thread_id": self.thread_id,
                        "turn_id": turn_id,
                        "terminal_type": "failed",
                        "message": "startup failed",
                    },
                )
            return super().latest_turn_event(candidate_turn_id)

    stream = PublishingStream()
    state, session_id, thread_id = _empty_turn_state(tmp_path, stream)
    stream.session_id = session_id
    stream.thread_id = thread_id

    async def receive() -> list[str]:
        return [item async for item in turn_sse(state, session_id, thread_id, turn_id)]

    events = asyncio.run(receive())
    assert len(events) == 1
    assert 'type="failed">startup failed</SSE>' in events[0]


def test_turn_sse_ignores_other_turn_terminal_while_waiting_for_target(tmp_path: Path) -> None:
    stream = MemoryRuntimeEventStream()
    state, session_id, thread_id = _empty_turn_state(tmp_path, stream)
    turn_id = "turn-target"

    async def receive() -> str:
        generator = turn_sse(state, session_id, thread_id, turn_id)
        pending = asyncio.create_task(anext(generator))
        await asyncio.sleep(0.1)
        publish_terminal(
            state,
            session_id=session_id,
            thread_id=thread_id,
            turn_id="turn-other",
            terminal_type="failed",
            message="other failure",
        )
        await asyncio.sleep(0.1)
        assert not pending.done()
        publish_terminal(
            state,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            terminal_type="failed",
            message="target failure",
        )
        try:
            return await asyncio.wait_for(pending, timeout=2.0)
        finally:
            await generator.aclose()

    event = asyncio.run(receive())
    assert f'id="{turn_id}" type="failed">target failure</SSE>' in event
    assert "other failure" not in event


def test_sse_reconnect_rebases_from_sqlite_and_emits_standard_cursor_ids(
    tmp_path: Path,
    event_stream: MemoryRuntimeEventStream,
) -> None:
    stream = event_stream
    paths, store, node, frame = _canonical_turn(tmp_path)
    state = SimpleNamespace(
        paths=paths,
        agent_thread_index=None,
        runtime_event_stream=stream,
        active_turn_streams={},
    )
    publish_frame(state, frame, node)
    first_cursor = stream.latest_thread_id(node.thread_id)

    final = node.clone()
    final.status = "success"
    final.timestamp = utc_iso()
    delta = NodeFrame.delta(node, final, revision=1)
    store.update_node_with_frame(final, delta)
    publish_frame(state, delta, final)
    publish_terminal(
        state,
        session_id=final.session_id,
        thread_id=final.thread_id,
        turn_id=final.id,
        terminal_type="success",
    )
    latest_cursor = stream.latest_thread_id(final.thread_id)

    async def reconnect() -> list[str]:
        return [
            item
            async for item in turn_sse(
                state,
                final.session_id,
                final.thread_id,
                final.id,
                last_event_id=first_cursor,
            )
        ]

    events = asyncio.run(reconnect())
    assert len(events) == 2
    assert events[0].startswith(f"id: {state.runtime_event_epoch}|{latest_cursor}\ndata: ")
    assert '"type":"turn.snapshot"' in events[0]
    assert events[1].startswith(f"id: {state.runtime_event_epoch}|{latest_cursor}\ndata: <SSE")
    assert 'type="success"' in events[1]

    async def thread_baseline() -> list[str]:
        generator = thread_sse(state, final.session_id, final.thread_id)
        try:
            return [await anext(generator), await anext(generator)]
        finally:
            await generator.aclose()

    thread_events = asyncio.run(thread_baseline())
    assert '"type":"thread.ready"' in thread_events[0]
    assert '"type":"turn.snapshot"' in thread_events[1]
    assert json.loads(thread_events[1].split("data: ", 1)[1])["current_turn_id"] == final.id


def test_turn_sse_waits_for_the_accepted_rewind_delivery_before_using_existing_turn(
    tmp_path: Path,
    event_stream: MemoryRuntimeEventStream,
) -> None:
    stream = event_stream
    paths, store, node, _frame = _canonical_turn(tmp_path)
    final = node.clone()
    final.status = "success"
    final.timestamp = utc_iso()
    store.finalize_node(final)
    state = SimpleNamespace(
        paths=paths,
        agent_thread_index=None,
        runtime_event_stream=stream,
        active_turn_streams={},
    )
    publish_terminal(
        state,
        session_id=final.session_id,
        thread_id=final.thread_id,
        turn_id=final.id,
        terminal_type="success",
    )

    async def receive_after_admission() -> str:
        generator = turn_sse(
            state,
            final.session_id,
            final.thread_id,
            final.id,
            delivery_id="rewind-delivery",
        )
        pending = asyncio.create_task(anext(generator))
        await asyncio.sleep(0.1)
        assert not pending.done()
        store.append_turn_version(
            final.id,
            {"type": "text", "text": "rewound", "status": "success"},
            delivery_id="rewind-delivery",
        )
        try:
            return await asyncio.wait_for(pending, timeout=2.0)
        finally:
            await generator.aclose()

    event = asyncio.run(receive_after_admission())
    assert '"type":"turn.snapshot"' in event
    assert '"delivery_id":"rewind-delivery"' in event
    assert "<SSE" not in event


@pytest.mark.parametrize("kind", ["thread", "turn"])
def test_slow_sse_reader_rebases_after_replay_eviction(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(event_stream_module, "EVENT_STREAM_MAXLEN", 2)
    stream = MemoryRuntimeEventStream()
    paths, store, node, frame = _canonical_turn(tmp_path)
    state = SimpleNamespace(paths=paths, agent_thread_index=None, runtime_event_stream=stream)
    publish_frame(state, frame, node)

    async def receive():
        subscription = (
            thread_sse(state, node.session_id, node.thread_id)
            if kind == "thread"
            else turn_sse(state, node.session_id, node.thread_id, node.id)
        )
        if kind == "thread":
            await anext(subscription)
        await anext(subscription)
        current = node
        for index in range(5):
            updated = current.clone()
            updated.data[0][-1]["content"] = [
                {"type": "text", "text": "".join(f"complete-{part}" for part in range(index + 1)), "status": "running"}
            ]
            delta = NodeFrame.delta(current, updated, revision=index + 1)
            store.update_node_with_frame(updated, delta)
            publish_frame(state, delta, updated)
            current = updated
        packet = await asyncio.wait_for(anext(subscription), 2)
        await subscription.aclose()
        return packet

    packet = asyncio.run(receive())
    assert '"type":"turn.snapshot"' in packet
    assert "complete-4" in packet
    stream.close()


def test_busy_thread_does_not_erase_another_retained_events_receipt(monkeypatch):
    monkeypatch.setattr(event_stream_module, "EVENT_STREAM_MAXLEN", 2)
    stream = MemoryRuntimeEventStream()
    first = dict(
        event_id="retained", turn_id="turn-a", thread_id="thread-a", sequence=1, payload={"type": "turn.delta"}
    )
    original = stream.publish(**first)
    for sequence in range(10):
        stream.publish(
            event_id=f"busy-{sequence}",
            turn_id="turn-b",
            thread_id="thread-b",
            sequence=sequence,
            payload={"type": "turn.delta"},
        )
    assert stream.publish(**first) == original
    assert len(stream.read_turn("turn-a", "0", block_ms=0)) == 1
    assert len(stream._events) == 3
    stream.close()
