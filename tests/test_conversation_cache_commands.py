from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.domain.runtime_state import RuntimeState
from backend.jobs import JobRegistry, JobScopeKind
from backend.jobs.output_buffer import OutputBuffer
from backend.tools.base import ConfirmationRequired, ToolError, ToolInvocationContext
from backend.tools.command import WorkspaceCommand
from backend.tools.default_tools.command import stdin_tool
from backend.tools.registry import ToolRegistry


def python_command(code: str) -> str:
    return f'"{sys.executable}" -u -c "{code}"'


@pytest.fixture
def commands(tmp_path):
    manager = WorkspaceCommand(tmp_path, terminal_type="cmd")
    registry = JobRegistry()
    scope = registry.root_scope().child(JobScopeKind.RUN, session_id="session", thread_id="thread", run_id="run")
    context = ToolInvocationContext(session_id="session", turn_id="turn", job_scope=scope)
    yield manager, context
    scope.close(timeout=5)
    manager.close()


def test_real_command_input_incremental_output_and_scope_cleanup(commands):
    manager, context = commands
    result = json.loads(
        manager.run_with_context(context, python_command("print('ready'); v=input(); print('received:'+v)"), 0)
    )
    session_id = result["session_id"]
    result = json.loads(manager.write_with_context(context, session_id, "yes\n", 5000))
    assert result["status"] == "succeeded"
    assert "received:yes" in result["output"]
    assert json.loads(manager.write_with_context(context, session_id, "", 0))["output"] == ""
    context.job_scope.close(timeout=5)
    with pytest.raises(ToolError, match="released"):
        manager.write_with_context(context, session_id, "", 0)


def test_real_command_big_stdout_and_stderr_are_bounded(commands):
    manager, context = commands
    result = json.loads(
        manager.run_with_context(
            context,
            python_command(
                "import sys; print('HEAD'); sys.stdout.write('x'*2200000); sys.stdout.flush(); print('TAIL',file=sys.stderr)"
            ),
            5000,
            100,
        )
    )
    assert result["exit_code"] == 0
    assert "HEAD" in result["output"] and "TAIL" in result["output"]
    assert "bytes omitted" in result["output"]
    for job, _, _ in manager._sessions.values():
        assert job.buffer.retained_bytes <= 1024 * 1024


def test_real_command_failure_preserves_exit_and_source_error(commands):
    manager, context = commands
    with pytest.raises(Exception) as caught:
        manager.run_with_context(
            context, python_command("import sys; print('failure',file=sys.stderr); sys.exit(7)"), 5000
        )
    payload = json.loads(caught.value.tool_output)
    assert payload["exit_code"] == 7
    assert "failure" in payload["output"]
    assert payload["error_report"]["type"] == type(caught.value).__name__


def test_real_command_ctrl_c_and_turn_ownership(commands):
    manager, context = commands
    result = json.loads(manager.run_with_context(context, python_command("import time; time.sleep(60)"), 0))
    with pytest.raises(ToolError, match="another Turn"):
        manager.write_with_context(ToolInvocationContext(turn_id="other"), result["session_id"], "", 0)
    try:
        stopped = json.loads(manager.write_with_context(context, result["session_id"], "\x03", 5000))
    except Exception as error:
        stopped = json.loads(error.tool_output)
    assert "exit_code" in stopped


def test_input_approval_only_for_nonempty_input(commands):
    manager, context = commands
    registry = ToolRegistry([stdin_tool(manager)])
    with pytest.raises(ConfirmationRequired):
        registry.invoke_with_context("write_stdin", {"session_id": "missing", "chars": "yes\n"}, context)
    with pytest.raises(ToolError, match="released"):
        registry.invoke_with_context("write_stdin", {"session_id": "missing"}, context)


def test_head_tail_utf8_and_absolute_reads():
    buffer = OutputBuffer(1024)
    buffer.append(("中文" * 1000).encode())
    output, cursor = buffer.read()
    assert "\ufffd" not in output
    assert buffer.retained_bytes <= 1024
    assert "bytes omitted" in output
    buffer.append(b"last")
    assert buffer.read(cursor)[0] == "last"


def test_http_history_pages_and_idle_cache_eviction(tmp_path: Path):
    state = WebAppState(tmp_path / "data")
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        sid, tid = sidebar["session_id"], sidebar["thread_id"]
        store = state.session_store
        parent = store.ensure_root_node(sid)
        ids = []
        for number in range(12):
            turn = RuntimeState.create(
                session_id=sid,
                thread_id=tid,
                id=f"turn-{number}",
                parent=parent,
                user_content=[{"type": "text", "text": str(number), "status": "success"}],
            )
            store.create_node(turn)
            turn.status = "success"
            store.finalize_node(turn)
            ids.append(turn.id)
            parent = turn
        first = client.get("/api/turns/history", params={"session_id": sid, "thread_id": tid}).json()
        assert [node["id"] for node in first["turns"]] == ids[-5:]
        second = client.get(
            "/api/turns/history", params={"session_id": sid, "thread_id": tid, "before": first["next_cursor"]}
        ).json()
        assert [node["id"] for node in second["turns"]] == ids[-10:-5]
        for number in range(6):
            state.conversation_cache.touch("other", f"idle-{number}")
        assert not state.conversation_cache.contains(tid)
        assert store.get_node(sid, ids[0]) is not None
        for number in range(8):
            state.conversation_cache.begin("active", f"active-{number}", f"running-{number}")
        state.conversation_cache.trim()
        assert len(state.conversation_cache.entries) == 13
        assert len(client.get("/api/turns/history", params={"session_id": sid, "thread_id": tid}).json()["turns"]) == 5


def test_branch_pages_inherit_history_without_loading_all_nodes(tmp_path, monkeypatch):
    from backend.storage.sqlite import SQLiteSessionStore

    state = WebAppState(tmp_path / "data")
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        sid = sidebar["session_id"]
        parent = state.session_store.ensure_root_node(sid)
        for number in range(8):
            turn = RuntimeState.create(
                session_id=sid,
                thread_id=sid,
                id=f"ancestry-{number}",
                parent=parent,
                user_content=[{"type": "text", "text": str(number), "status": "success"}],
            )
            state.session_store.create_node(turn)
            turn.status = "success"
            state.session_store.finalize_node(turn)
            parent = turn
        forked = client.post("/api/turns/ancestry-7/fork", json={}).json()
        branch = forked["sidebar_thread"]["thread_id"]

        def forbidden(*_args, **_kwargs):
            raise RuntimeError("History display loaded all nodes")

        monkeypatch.setattr(SQLiteSessionStore, "load_nodes", forbidden)
        first = client.get("/api/turns/history", params={"session_id": sid, "thread_id": branch})
        assert first.status_code == 200, first.text
        page = first.json()
        assert len(page["turns"]) == 5 and page["has_more"]
        second = client.get(
            "/api/turns/history", params={"session_id": sid, "thread_id": branch, "before": page["next_cursor"]}
        ).json()
        assert second["turns"][0]["id"] == "ancestry-0"
        assert not second["has_more"]


def test_queued_conversation_stays_retained_until_last_message_is_deleted(tmp_path):
    from backend.domain import QueuedMessage

    state = WebAppState(tmp_path / "data")
    state.turn_message_worker.close()
    try:
        state.conversation_cache.touch("session", "queued")
        state.message_queue.create(QueuedMessage("message", "queued", "pending work"))
        for number in range(6):
            state.conversation_cache.touch("session", f"idle-{number}")
        assert state.conversation_cache.contains("queued")
        assert len(state.conversation_cache.entries) == 6
        state.message_queue.delete("queued", "message")
        assert len(state.conversation_cache.entries) == 5
    finally:
        state.close()


def test_utf8_head_boundary_does_not_discard_output_before_overflow():
    buffer = OutputBuffer(1024)
    payload = ("a" * 511 + "中文").encode("utf-8")
    buffer.append(payload)
    assert buffer.read()[0] == payload.decode("utf-8")


def test_history_cursor_allows_new_turn_but_rejects_rewind(tmp_path):
    state = WebAppState(tmp_path / "data")
    state.turn_message_worker.close()
    try:
        store = state.session_store
        sid = store.create_session("cursor").session_id
        parent = store.ensure_root_node(sid)
        for number in range(8):
            node = RuntimeState.create(
                session_id=sid,
                thread_id=sid,
                id=f"cursor-{number}",
                parent=parent,
                user_content=[{"type": "text", "text": str(number), "status": "success"}],
            )
            store.create_node(node)
            node.status = "success"
            store.finalize_node(node)
            parent = node
        _, cursor = store.load_turn_page(sid, sid)
        child = RuntimeState.create(
            session_id=sid,
            thread_id=sid,
            id="cursor-new",
            parent=parent,
            user_content=[{"type": "text", "text": "new", "status": "success"}],
        )
        store.create_node(child)
        child.status = "success"
        store.finalize_node(child)
        assert len(store.load_turn_page(sid, sid, before=cursor)[0]) == 3
        store.append_turn_version("cursor-4", {"type": "text", "text": "rewind", "status": "success"})
        with pytest.raises(ValueError, match="history changed"):
            store.load_turn_page(sid, sid, before=cursor)
    finally:
        state.close()


def test_output_source_switches_remain_visible():
    buffer = OutputBuffer()
    buffer.append(b"one", "stdout")
    buffer.append(b"two", "stderr")
    buffer.append(b"three", "stdout")
    assert buffer.read()[0] == "one\n[stderr] two\n[stdout] three"


def test_evicted_acknowledged_messages_keep_identity_without_bodies():
    from backend.storage.message_queue import MemoryMessageQueue
    from tests.test_process_memory import envelope

    queue = MemoryMessageQueue()
    original = envelope()
    queue.dispatch_turn_start(original)
    claimed = queue.claim_turn_start("worker")
    queue.ack(claimed)
    queue.release_thread_cache("thread", ("turn",))
    duplicate = queue.dispatch_turn_start(original)
    assert duplicate.target_id == original.target_id
    assert duplicate.delivery_id == original.delivery_id
    assert not hasattr(duplicate, "payload")
    assert queue.claim_turn_start("worker") is None


def test_real_utf8_output_and_lease_live_until_exit(commands, tmp_path):
    from dataclasses import replace

    from backend.sandbox import (
        FileAccessMode,
        NetworkMode,
        ResourceLimits,
        SandboxExecutionDecision,
        SandboxMaintenanceBusy,
    )
    from tests.testing_sandbox import DirectTestSandboxLauncher

    manager, original = commands
    launcher = DirectTestSandboxLauncher()
    launcher.terminate_tree = None
    decision = SandboxExecutionDecision(
        launcher=launcher,
        workspaces=(tmp_path,),
        session_id="session",
        user_id="local",
        file_mode=FileAccessMode.READ_ONLY,
        network_mode=NetworkMode.NO_NETWORK,
        network_allowlist=(),
        proxy_port=17831,
        limits=ResourceLimits(wall_seconds=15),
    )
    context = replace(original, sandbox_decision=decision)
    result = json.loads(
        manager.run_with_context(
            context,
            python_command(
                "v=input(); import sys; sys.stdout.buffer.write(('\\u4e2d\\u6587'*300000).encode('utf-8')); sys.stdout.flush()"
            ),
            0,
        )
    )
    assert launcher.maintenance_gate.active_commands == 1
    with pytest.raises(SandboxMaintenanceBusy):
        launcher.maintenance_gate.acquire_maintenance()
    result = json.loads(manager.write_with_context(context, result["session_id"], "go\n", 5000, 100))
    assert result["exit_code"] == 0
    assert "\ufffd" not in result["output"]
    assert result["cache_omitted_bytes"] > 0
    assert launcher.maintenance_gate.active_commands == 0
    with launcher.maintenance_gate.acquire_maintenance():
        pass


def test_turn_close_stops_real_process_tree_and_wakes_output_waiter(commands):
    from concurrent.futures import ThreadPoolExecutor

    from tests.test_job_subprocess import _pid_alive, wait_until

    manager, context = commands
    result = json.loads(
        manager.run_with_context(
            context,
            python_command(
                "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
            ),
            1000,
        )
    )
    child_pid = int(result["output"].strip())
    with ThreadPoolExecutor(max_workers=1) as executor:

        def wait_for_output():
            try:
                return manager.write_with_context(context, result["session_id"], "", 60000)
            except Exception as error:
                return str(error)

        waiting = executor.submit(wait_for_output)
        context.job_scope.close(timeout=5)
        assert waiting.result(timeout=5) is not None
    wait_until(lambda: not _pid_alive(child_pid), timeout=5)
    assert manager._sessions == {}


def test_output_loss_counts_and_token_budget_do_not_replay(commands):
    from backend.providers.token_usage import _encode_length
    from backend.tools.command_output import limit_output

    buffer = OutputBuffer(1024)
    buffer.append(b"a" * 2000)
    text, cursor, omitted = buffer.read_details()
    assert omitted == 976
    assert text.startswith("a" * 512) and text.endswith("a" * 512)
    assert buffer.read(cursor) == ("", cursor)
    limited, omitted = limit_output("hello " * 10000, 30)
    assert _encode_length(limited, "unknown") <= 30
    assert omitted > 0
    assert limit_output("hello " * 10000, 1)[1] > 0
    manager, context = commands
    result = json.loads(manager.run_with_context(context, python_command("print('abcdef '*10000)"), 5000, 30))
    assert result["output_truncated"] and result["response_omitted_bytes"] > 0
    job_id = next(iter(manager._sessions))
    assert json.loads(manager.write_with_context(context, job_id, "", 0))["output"] == ""


def test_idle_eviction_releases_todo_events_and_stops_connected_sse(tmp_path):
    import asyncio

    from backend.api.runtime_event_transport import thread_sse

    state = WebAppState(tmp_path / "data")
    state.turn_message_worker.close()
    session = state.session_store.create_session("Cache ownership")
    sid = session.session_id
    state.conversation_cache.touch(sid, sid, ["cached-turn"])
    state.todo_store.update(
        session_id=sid,
        turn_id="cached-turn",
        call_id="todo-call",
        expected_revision=0,
        operations=[{"op": "add", "content": "temporary", "status": "pending"}],
    )
    state.runtime_event_stream.publish(
        event_id="cached-event", turn_id="cached-turn", thread_id=sid, sequence=1, payload={"type": "test"}
    )

    async def exercise():
        stream = thread_sse(state, sid, sid)
        assert "thread.ready" in await anext(stream)
        for index in range(6):
            state.conversation_cache.touch("idle", f"idle-{index}")
        assert "thread.evicted" in await anext(stream)
        await stream.aclose()

    try:
        asyncio.run(exercise())
        assert not state.runtime_event_stream.has_event("cached-event")
        assert state.todo_store.snapshot(sid, "cached-turn").revision == 0
        assert not state.conversation_cache.contains(sid)
        assert state.session_store.get_session(sid) is not None
    finally:
        state.close()
