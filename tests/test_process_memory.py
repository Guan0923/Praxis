from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.domain import MessageEnvelope, MessageQueueUnavailable, QueuedMessage
from backend.domain.runtime_state import RuntimeState
from backend.storage.message_queue import MemoryMessageQueue
from backend.storage.runtime_event_stream import MemoryRuntimeEventStream
from backend.storage.sqlite import SQLiteSessionStore
from backend.storage.terminal_stream import MemoryTerminalOutputStream


def envelope(delivery_id: str = "delivery") -> MessageEnvelope:
    return MessageEnvelope(
        delivery_id, "user", "thread", "turn_start", "turn", "session", "thread", {"content": "hello"}, (delivery_id,)
    )


def test_queue_waits_until_delivery_and_close_wakes_waiters() -> None:
    queue = MemoryMessageQueue()
    with ThreadPoolExecutor(2) as pool:
        waiting = pool.submit(queue.wait_turn_start, "worker", Event())
        queue.dispatch_turn_start(envelope())
        claimed = waiting.result(timeout=2)
        assert claimed.envelope.delivery_id == "delivery"
        queue.ack(claimed)
        stopped = pool.submit(queue.wait_turn_start, "worker", Event())
        queue.close()
        assert stopped.result(timeout=2) is None
    with pytest.raises(MessageQueueUnavailable):
        queue.dispatch_turn_start(envelope("late"))


def test_worker_failure_can_retry_without_a_second_delivery() -> None:
    queue = MemoryMessageQueue()
    delivery = envelope()
    queue.dispatch_turn_start(delivery)
    first = queue.claim_turn_start("worker")
    queue.retry(first)
    second = queue.claim_turn_start("worker")
    assert first.stream_id == second.stream_id
    assert second.envelope.attempts == 2
    queue.ack(second)
    queue.dispatch_turn_start(delivery)
    assert queue.claim_turn_start("worker") is None


def test_event_waiters_receive_independent_payloads_and_close_wakes_them() -> None:
    stream = MemoryRuntimeEventStream()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(stream.read_thread, "thread", "0", block_ms=5000)
        second = pool.submit(stream.read_thread, "thread", "0", block_ms=5000)
        source = {"nested": {"text": "original"}}
        stream.publish(event_id="event", turn_id="turn", thread_id="thread", sequence=1, payload=source)
        left, right = first.result(timeout=2), second.result(timeout=2)
        source["nested"]["text"] = "mutated"
        left[0].payload["nested"]["text"] = "another mutation"
        assert right[0].payload["nested"]["text"] == "original"
        waiting = pool.submit(stream.read_thread, "thread", "1", block_ms=5000)
        stream.close()
        assert waiting.result(timeout=2) == []


def test_terminal_unicode_chunks_are_complete_and_isolated() -> None:
    output = MemoryTerminalOutputStream()
    text = "\U0001f600" * 10000
    chunks = output.append("terminal-a", text)
    assert "".join(chunk.data for chunk in chunks) == text
    assert all(len(chunk.data.encode("utf-8")) <= 16384 for chunk in chunks)
    assert output.after("terminal-b", 0) == []
    output.close()
    assert output.after("terminal-a", 0) == []


def test_normal_shutdown_discards_queue_and_saves_interrupted_history(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "data")
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        sid = sidebar["session_id"]
        store = session_store(state)
        root = store.ensure_root_node(sid)
        node = RuntimeState.create(
            session_id=sid,
            thread_id=sidebar["thread_id"],
            id="unfinished",
            parent=root,
            user_content=[{"type": "text", "text": "saved history", "status": "success"}],
        )
        store.create_node(node)
        state.message_queue.create(QueuedMessage("queued", sidebar["thread_id"], "discard this"))
        assert store.get_node(sid, node.id).status == "running"
    assert state.message_queue.list(sidebar["thread_id"]) == []
    saved = SQLiteSessionStore(state.paths).get_node(sid, node.id)
    assert saved.status == "failed"
    assert "saved history" in str(saved.to_dict())
    assert "backend_process_stopped" in str(saved.to_dict())
    reopened = WebAppState(tmp_path / "data")
    try:
        assert reopened.message_queue.list(sidebar["thread_id"]) == []
        assert reopened.session_store.get_node(sid, node.id).status == "failed"
    finally:
        reopened.close()


def test_real_process_exit_leaves_history_but_no_messages_to_restore(tmp_path: Path) -> None:
    script = r"""
import os, sys
from pathlib import Path
from backend.api.state import WebAppState
from backend.domain import QueuedMessage
from backend.domain.runtime_state import RuntimeState
state = WebAppState(Path(sys.argv[1]))
state.turn_message_worker.close()
store = state.session_store
session = store.create_session("crash test")
root = store.ensure_root_node(session.session_id)
turn = RuntimeState.create(session_id=session.session_id, thread_id=session.session_id, id="crashed-turn", parent=root, user_content=[{"type": "text", "text": "persisted before exit", "status": "success"}])
store.create_node(turn)
state.message_queue.create(QueuedMessage("lost", session.session_id, "not persisted"))
print(session.session_id, flush=True)
os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "data")], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 17, result.stderr
    sid = result.stdout.strip()
    state = WebAppState(tmp_path / "data")
    try:
        assert state.message_queue.list(sid) == []
        assert state.session_store.get_node(sid, "crashed-turn").status == "running"
        state.access_session(sid)
        assert state.session_store.get_node(sid, "crashed-turn").status == "failed"
        assert state.subagent_coordinator._jobs == {}
    finally:
        state.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY integration")
def test_real_terminal_reads_output_without_external_cache(tmp_path):
    from time import monotonic

    from backend.api.terminal_manager import TerminalManager

    manager = TerminalManager()
    try:
        terminal = manager.create("cmd", str(tmp_path))
        manager.write(terminal.id, "echo MEMORY_TEST_ONE & echo MEMORY_TEST_TWO\r\n")
        sequence = 0
        output = ""
        deadline = monotonic() + 10
        while monotonic() < deadline:
            chunks = manager.wait_after(terminal.id, sequence, timeout=0.2)
            for chunk in chunks:
                output += chunk.data
                sequence = chunk.sequence
            if "MEMORY_TEST_ONE \r\nMEMORY_TEST_TWO" in output or "MEMORY_TEST_ONE\r\nMEMORY_TEST_TWO" in output:
                break
        else:
            pytest.fail("PTY did not return the command's two output lines")
        assert manager.output.after(terminal.id, 0)
    finally:
        manager.close_all()
    assert manager.output.after(terminal.id, 0) == []
