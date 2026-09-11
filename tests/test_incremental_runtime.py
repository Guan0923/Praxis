from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from backend.api.runtime_event_transport import _cursor_id, _resume_cursor, _runtime_snapshot, publish_frame, turn_sse
from backend.configuration import ClientPaths
from backend.domain import TracePersistenceError
from backend.domain.input_message import InputMessage
from backend.domain.runtime_state import NodeWriter, RuntimeState
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.runtime.persistence.streaming import RuntimeFramePersistence
from backend.storage.runtime_event_stream import MemoryRuntimeEventStream
from backend.storage.sqlite import SQLiteSessionStore


def new_turn(tmp_path: Path, store_type=SQLiteSessionStore):
    store = store_type(ClientPaths(tmp_path / "data"))
    session = store.create_session("incremental")
    store.create_sidebar_thread(session_id=session.session_id, thread_id=session.session_id, title="incremental")
    root = store.ensure_root_node(session.session_id)
    node = RuntimeState.create(
        session_id=session.session_id,
        thread_id=session.session_id,
        parent=root,
        user_content=[{"type": "text", "text": "hello", "status": "success"}],
    )
    return store, node


def test_text_writes_are_deltas_and_all_store_readers_reconstruct_them(tmp_path: Path) -> None:
    store, node = new_turn(tmp_path)
    frames = []
    writer = NodeWriter(store, emit=frames.append)
    node = writer.create(node)
    node = writer.append_item(node, {"type": "text", "text": "start", "status": "running"})
    previous = node
    for chunk in [" one", " two", " three"]:
        node = writer.append_text(node, data_idx=0, item_idx=0, delta=chunk, persist=True)
    assert previous.assistant_items[0]["text"] == "start"
    with pytest.raises(TypeError, match="immutable"):
        frames[-1].operations[0]["delta"] = "changed"
    frame_payload = frames[-1].to_dict()
    frame_payload["operations"][0]["delta"] = "independent serialization"
    assert frames[-1].operations[0]["delta"] == " three"
    with sqlite3.connect(store.paths.session_db(node.session_id)) as connection:
        baseline = json.loads(
            connection.execute(
                "SELECT payload_json FROM json_objects WHERE namespace='runtime_node' AND object_id=?",
                (node.id,),
            ).fetchone()[0]
        )
        assert baseline["data"][0][1]["content"] == []
        journal = connection.execute(
            "SELECT payload_json FROM json_objects WHERE namespace=? ORDER BY object_id",
            (f"runtime_delta:{node.id}",),
        ).fetchall()
        assert len(journal) == 4
        assert "start one two" not in journal[-1][0]
    expected = "start one two three"
    assert store.get_node(node.session_id, node.id).assistant_items[0]["text"] == expected
    assert (
        next(item for item in store.load_nodes(node.session_id) if item.id == node.id).assistant_items[0]["text"]
        == expected
    )
    assert store.get_node(node.session_id, node.id).assistant_items[0]["text"] == expected
    node = writer.set_item_status(node, data_idx=0, message_idx=1, item_idx=0, status="success")
    final = writer.finalize(node, "success")
    assert store.get_node(node.session_id, node.id).to_dict() == final.to_dict()
    with sqlite3.connect(store.paths.session_db(node.session_id)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM json_objects WHERE namespace=?", (f"runtime_delta:{node.id}",)
            ).fetchone()[0]
            == 0
        )


class GatedStore(SQLiteSessionStore):
    def __init__(self, paths):
        super().__init__(paths)
        self.started = Event()
        self.release = Event()
        self.fail = False

    def append_runtime_delta(self, frame, *, thread_id, status):
        if any(op["op"] == "append_text" for op in frame.operations):
            self.started.set()
            if not self.release.wait(5):
                raise TimeoutError("test did not release persistence")
            if self.fail:
                raise OSError("simulated disk failure")
        super().append_runtime_delta(frame, thread_id=thread_id, status=status)


def test_display_and_reconnect_do_not_wait_for_storage_but_completion_does(tmp_path: Path) -> None:
    store, node = new_turn(tmp_path, GatedStore)
    stream = MemoryRuntimeEventStream()
    state = SimpleNamespace(paths=store.paths, runtime_event_stream=stream, active_turn_streams={node.id: object()})
    persistence = RuntimeFramePersistence(store)
    writer = NodeWriter(
        store,
        emit=lambda frame: publish_frame(state, frame, writer.view(frame.session_id, frame.turn_id)),
        persist_delta=persistence.submit,
        flush_persistence=persistence.flush,
    )
    try:
        node = writer.create(node)
        node = writer.append_item(node, {"type": "text", "text": "start", "status": "running"})
        persistence.flush()
        node = writer.append_text(node, data_idx=0, item_idx=0, delta=" visible", persist=True)
        assert store.started.wait(2)
        assert stream.latest_turn_event(node.id).payload["operations"][0]["delta"] == " visible"
        assert store.get_node(node.session_id, node.id).assistant_items[0]["text"] == "start"
        state.live_turn_bridges = {(node.session_id, node.id): SimpleNamespace(writer=writer, closed=False)}
        live, sequence = _runtime_snapshot(state, store, node.session_id, node.id)
        assert live.assistant_items[0]["text"] == "start visible"
        assert sequence > store.runtime_event_sequence(node.session_id, node.id)

        async def reconnect():
            events = turn_sse(state, node.session_id, node.thread_id, node.id)
            try:
                return await anext(events)
            finally:
                await events.aclose()

        assert "start visible" in asyncio.run(reconnect())
        node = writer.set_item_status(node, data_idx=0, message_idx=1, item_idx=0, status="success")
        with ThreadPoolExecutor(max_workers=1) as pool:
            finalizing = pool.submit(writer.finalize, node, "success")
            assert not finalizing.done()
            store.release.set()
            final = finalizing.result(timeout=5)
        assert final.status == "success"
        assert store.get_node(node.session_id, node.id).assistant_items[0]["text"] == "start visible"
    finally:
        store.release.set()
        persistence.close()


def test_persistence_failure_is_reported_and_never_emits_success(tmp_path: Path) -> None:
    store, node = new_turn(tmp_path, GatedStore)
    store.fail = True
    store.release.set()
    frames = []
    persistence = RuntimeFramePersistence(store)
    writer = NodeWriter(
        store, emit=frames.append, persist_delta=persistence.submit, flush_persistence=persistence.flush
    )
    node = writer.create(node)
    node = writer.append_item(node, {"type": "text", "text": "", "status": "running"})
    node = writer.append_text(node, data_idx=0, item_idx=0, delta="visible", persist=True)
    with pytest.raises(TracePersistenceError):
        persistence.flush()
    with pytest.raises(TracePersistenceError):
        writer.finalize(node, "success")
    assert not any(frame.patch.get("status") == "success" for frame in frames)
    with pytest.raises(TracePersistenceError):
        persistence.close()


def test_old_execution_cursor_is_not_replayed_after_restart() -> None:
    old = SimpleNamespace()
    current = SimpleNamespace()
    cursor = _cursor_id(old, "12")
    assert _resume_cursor(old, cursor, "13") == "12"
    assert _resume_cursor(current, cursor, "13") == "13"


def test_finishing_an_item_does_not_mutate_a_previously_emitted_view(tmp_path: Path) -> None:
    store, node = new_turn(tmp_path)
    views = []
    bridge = RuntimeEventNodeBridge(
        store,
        session_id=node.session_id,
        message=InputMessage.from_input("hello"),
        emit=lambda frame: views.append(bridge.writer.view(frame.session_id, frame.turn_id)),
    )
    bridge.start()
    bridge._update_stream_item("text", "one")
    bridge._update_stream_item("text", "two")
    previous = views[-1]
    bridge._finish_stream_item()
    assert previous.assistant_items[0] == {"type": "text", "text": "onetwo", "status": "running"}
    assert bridge.finish("success").status == "success"


def test_reopened_store_recovers_committed_delta_without_an_outbox(tmp_path: Path) -> None:
    store, node = new_turn(tmp_path)
    frames = []
    writer = NodeWriter(store, emit=frames.append)
    node = writer.create(node)
    node = writer.append_item(node, {"type": "text", "text": "committed", "status": "running"})
    reopened = SQLiteSessionStore(store.paths)
    recovered, sequence = reopened.runtime_stream_snapshot(node.session_id, node.id)
    assert recovered.assistant_items[0]["text"] == "committed"
    assert sequence == frames[-1].sequence
    paused = reopened.pause_turn(node.id)
    assert paused.status == "paused"
    assert paused.assistant_items[0]["status"] == "failed"


def test_bounded_queue_applies_backpressure_without_losing_text(tmp_path: Path) -> None:
    store, node = new_turn(tmp_path, GatedStore)
    persistence = RuntimeFramePersistence(store, capacity=1)
    writer = NodeWriter(
        store, emit=lambda frame: None, persist_delta=persistence.submit, flush_persistence=persistence.flush
    )
    try:
        node = writer.create(node)
        node = writer.append_item(node, {"type": "text", "text": "", "status": "running"})
        persistence.flush()
        node = writer.append_text(node, data_idx=0, item_idx=0, delta="one", persist=True)
        assert store.started.wait(2)
        node = writer.append_text(node, data_idx=0, item_idx=0, delta="two", persist=True)
        with ThreadPoolExecutor(1) as pool:
            entered = Event()

            def append():
                entered.set()
                return writer.append_text(node, data_idx=0, item_idx=0, delta="three", persist=True)

            pending = pool.submit(append)
            assert entered.wait(2)
            assert not pending.done()
            store.release.set()
            pending.result(timeout=5)
        persistence.flush()
        assert store.get_node(node.session_id, node.id).assistant_items[0]["text"] == "onetwothree"
    finally:
        store.release.set()
        persistence.close()


def test_killed_process_recovers_only_committed_text(tmp_path: Path) -> None:
    script = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, "tests")
from test_incremental_runtime import new_turn, GatedStore, NodeWriter, RuntimeFramePersistence
store, node = new_turn(Path(sys.argv[1]), GatedStore)
persistence = RuntimeFramePersistence(store)
writer = NodeWriter(store, emit=lambda frame: None, persist_delta=persistence.submit, flush_persistence=persistence.flush)
node = writer.create(node)
node = writer.append_item(node, {"type": "text", "text": "committed", "status": "running"})
persistence.flush()
node = writer.append_text(node, data_idx=0, item_idx=0, delta=" lost tail", persist=True)
store.started.wait(2)
print(json.dumps({"session_id": node.session_id, "turn_id": node.id}), flush=True)
time.sleep(30)
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        identity = json.loads(child.stdout.readline())
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    recovered, _sequence = store.runtime_stream_snapshot(identity["session_id"], identity["turn_id"])
    assert recovered.assistant_items[0]["text"] == "committed"
    assert store.pause_turn(recovered.id).status == "paused"
