from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.domain import QueuedMessage, QueueItemStateConflict
from backend.sandbox.runtime.aggregate import AggregateResources


def test_delete_direct_metadata_without_history_or_session_scan(tmp_path, monkeypatch):
    state = WebAppState(tmp_path)
    store = state.session_store
    session = store.create_session("delete me")
    sid = session.session_id
    store.create_sidebar_thread(session_id=sid, thread_id=sid, title="delete me")
    state.message_queue.create(QueuedMessage("message", sid, "queued"))
    monkeypatch.setattr(store, "list_sessions", lambda **_: pytest.fail("delete scanned sessions"))
    monkeypatch.setattr(store, "_objects", lambda *_: pytest.fail("delete read history"))
    monkeypatch.setattr(store, "sidebar_thread_summary", lambda *_: pytest.fail("delete built summary"))
    app = create_app(state=state)
    try:
        with TestClient(app) as client:
            response = client.delete(f"/api/sidebar-threads/{sid}", params={"session_id": sid})
            assert response.status_code == 204, response.text
            assert response.content == b""
            assert store.get_sidebar_thread(sid, session_id=sid).deleted_at
            assert state.message_queue.list(sid) == []
            with pytest.raises(QueueItemStateConflict):
                state.message_queue.create(QueuedMessage("late", sid, "late"))
            assert client.delete(f"/api/sidebar-threads/{sid}", params={"session_id": sid}).status_code == 204
    finally:
        state.close()


def test_aggregate_fifo_real_usage_and_newest_first():
    manager = AggregateResources(start=False)
    manager.configure("backend", {"memory_mib": 64, "processes": 10, "handles": 100})
    values = [{"memory_bytes": 1024, "processes": 1, "handles": 1} for _ in range(3)]
    stopped = []
    try:
        for i, value in enumerate(values):
            ticket = str(i)
            assert manager.acquire("backend", ticket)["granted"]
            manager.register("backend", ticket, ticket, lambda v=value: dict(v), lambda reason, n=i: stopped.append(n))
        assert len(manager.jobs) == 3
        values[0]["memory_bytes"] = 65 * 1048576
        manager.tick()
        assert stopped == [2]
        assert not manager.acquire("backend", "fourth")["granted"]
        values[2].update(memory_bytes=0, processes=0, handles=0)
        manager.tick()
        assert stopped == [2, 1]
        values[0]["memory_bytes"] = 1024
        values[1].update(memory_bytes=0, processes=0, handles=0)
        manager.tick()
        assert manager.acquire("backend", "fourth")["granted"]
        assert not manager.acquire("backend", "fifth")["granted"]
        manager.cancel("backend", "fourth")
        assert manager.acquire("backend", "fifth")["granted"]
    finally:
        manager.close()


def test_aggregate_sampling_failure_is_not_zero_usage():
    manager = AggregateResources(start=False)
    failed = False

    def sample():
        if failed:
            raise OSError("accounting unavailable")
        return {"memory_bytes": 1000, "processes": 1, "handles": 1}

    manager.acquire("owner", "ticket")
    manager.register("owner", "ticket", "job", sample, lambda _: None)
    failed = True
    manager.tick()
    status = manager.acquire("owner", "next")
    assert not status["granted"]
    assert status["usage"]["memory_bytes"] == 1000
    assert status["error_report"]["type"] == "OSError"
    manager.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object")
def test_real_job_objects_three_commands_and_newest_termination():
    import win32api
    import win32con
    import win32event
    import win32process

    from backend.sandbox.native_windows.jobs import WindowsJobObject
    from backend.sandbox.policy import ResourceLimits

    manager = AggregateResources(start=False)
    manager.configure("owner", {"memory_mib": 512, "processes": 10, "handles": 20000})
    jobs, processes, stopped = [], [], []
    try:
        for i in range(3):
            job = WindowsJobObject(f"praxis-resource-test-{os.getpid()}-{i}", ResourceLimits())
            jobs.append(job)
            command = subprocess.list2cmdline([sys.executable, "-c", "import time; time.sleep(60)"])
            process, thread, pid, tid = win32process.CreateProcess(
                None,
                command,
                None,
                None,
                False,
                win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
                None,
                None,
                win32process.STARTUPINFO(),
            )
            processes.append(process)
            job.assign(process)
            win32process.ResumeThread(thread)
            thread.Close()
            assert manager.acquire("owner", str(i))["granted"]

            def stop(reason, j=job, index=i):
                stopped.append(index)
                j.terminate()

            manager.register("owner", str(i), str(i), job.usage, stop)
        time.sleep(0.5)
        manager.tick()
        total = manager.status("owner")["usage"]["processes"]
        assert total >= 3
        remaining = total - jobs[2].usage()["processes"]
        manager.configure("owner", {"memory_mib": 512, "processes": remaining, "handles": 20000})
        manager.tick()
        assert stopped == [2]
        assert win32event.WaitForSingleObject(processes[2], 5000) == win32con.WAIT_OBJECT_0
        assert win32process.GetExitCodeProcess(processes[0]) == 259
        manager.tick()
        assert manager.status("owner")["usage"]["processes"] == remaining
    finally:
        manager.close()
        for job in jobs:
            job.terminate()
            job.close()
        for process in processes:
            win32event.WaitForSingleObject(process, 5000)
            win32api.CloseHandle(process)


def test_delete_running_command_preserves_other_thread(tmp_path):
    import json

    from backend.jobs import JobScopeKind
    from backend.tools.base import ToolInvocationContext
    from backend.tools.command import WorkspaceCommand

    state = WebAppState(tmp_path / "data")
    store = state.session_store
    sid = store.create_session("running").session_id
    store.create_sidebar_thread(session_id=sid, thread_id=sid, title="running")
    other = store.create_session("other").session_id
    store.create_sidebar_thread(session_id=other, thread_id=other, title="other")
    manager = WorkspaceCommand(tmp_path, terminal_type="cmd")
    scope = (
        state.job_registry.root_scope()
        .child(JobScopeKind.SESSION, session_id=sid)
        .child(JobScopeKind.THREAD, thread_id=sid)
    )
    context = ToolInvocationContext(session_id=sid, turn_id="turn", job_scope=scope)
    command = f'"{sys.executable}" -c "import time; time.sleep(60)"'
    try:
        result = json.loads(manager.run_with_context(context, command, 0))
        assert result["session_id"]
        with TestClient(create_app(state)) as client:
            started = time.monotonic()
            assert client.delete(f"/api/sidebar-threads/{sid}", params={"session_id": sid}).status_code == 204
            print(f"delete running HTTP seconds: {time.monotonic() - started:.3f}")
            deadline = time.monotonic() + 10
            while state.conversation_deletion.result(sid)["status"] == "pending" and time.monotonic() < deadline:
                time.sleep(0.02)
            assert state.conversation_deletion.result(sid) == {"status": "completed"}
            assert state.job_registry.active_count(scope=scope) == 0
            assert store.get_sidebar_thread(other, session_id=other).deleted_at is None
    finally:
        manager.close()
        state.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object")
def test_real_job_current_memory_falls_after_release():
    import win32con
    import win32event
    import win32process

    from backend.sandbox.native_windows.jobs import WindowsJobObject
    from backend.sandbox.policy import ResourceLimits

    job = WindowsJobObject(f"praxis-memory-test-{os.getpid()}", ResourceLimits())
    command = subprocess.list2cmdline(
        [
            sys._base_executable,
            "-c",
            "import time,gc; x=bytearray(32*1024*1024); time.sleep(2); del x; gc.collect(); time.sleep(60)",
        ]
    )
    process, thread, _, _ = win32process.CreateProcess(
        None,
        command,
        None,
        None,
        False,
        win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
        None,
        None,
        win32process.STARTUPINFO(),
    )
    try:
        job.assign(process)
        win32process.ResumeThread(thread)
        thread.Close()
        high = 0
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            usage = job.usage()
            current = usage["memory_bytes"]
            high = max(high, current)
            if high > 32 * 1048576 and current < high - 24 * 1048576:
                assert usage["peak_memory_bytes"] >= high
                return
            time.sleep(0.05)
        pytest.fail(f"Current memory did not fall after freeing allocation; peak={high}")
    finally:
        job.terminate()
        win32event.WaitForSingleObject(process, 5000)
        process.Close()
        job.close()


def test_resource_wait_cancel_removes_fifo_ticket(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from backend.sandbox.runtime.launcher import SandboxLauncher

    manager = AggregateResources(start=False)
    manager.configure("owner", {"memory_mib": 64, "processes": 1, "handles": 100})
    manager.acquire("owner", "first")
    manager.register("owner", "first", "job", lambda: {"memory_bytes": 1, "processes": 1, "handles": 1}, lambda _: None)

    class Broker:
        def resource_request(self, operation, **values):
            if operation == "resource_cancel":
                manager.cancel("owner", values["ticket"])
                return {}
            return manager.acquire("owner", values["ticket"])

    launcher = SandboxLauncher(broker=Broker(), lease_store_path=tmp_path / "leases.json")
    cancel, notified = Event(), Event()
    try:
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(
                launcher.wait_resources, "second", cancelled=cancel.is_set, notify=lambda _: notified.set()
            )
            assert notified.wait(2)
            assert launcher.maintenance_gate.active_commands == 0
            cancel.set()
            with pytest.raises(InterruptedError):
                waiting.result(timeout=2)
        assert manager.status("owner")["queued"] == 0
    finally:
        manager.close()


def test_failed_termination_keeps_usage_and_blocks_admission():
    manager = AggregateResources(start=False)
    usage = {"memory_bytes": 1, "processes": 1, "handles": 1}

    def fail_stop(reason):
        raise OSError("termination failed")

    manager.acquire("owner", "first")
    manager.register("owner", "first", "job", lambda: dict(usage), fail_stop)
    manager.configure("owner", {"memory_mib": 64, "processes": 1, "handles": 10})
    usage["processes"] = 2
    manager.tick()
    status = manager.acquire("owner", "second")
    assert not status["granted"]
    assert status["usage"]["processes"] == 2
    assert status["error_report"]["message"] == "termination failed"
    assert "job" in manager.jobs
    manager.close()


def test_delete_blocks_pending_work_and_signals_running_thread():
    from threading import Event

    from backend.jobs import AdmissionPolicy, JobLane, JobRegistry, JobScopeKind, JobState, ThreadJob

    registry = JobRegistry()
    scope = (
        registry.root_scope()
        .child(JobScopeKind.SESSION, session_id="session")
        .child(JobScopeKind.THREAD, thread_id="thread")
    )
    entered = Event()
    release = Event()

    def target():
        entered.set()
        release.wait(5)

    running = ThreadJob("running", target)
    pending = ThreadJob("pending", lambda: pytest.fail("Deleted pending job started"))
    try:
        scope.submit(running, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        assert entered.wait(2)
        scope.register(pending, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        registry.block_threads("session", {"thread"})
        assert running.is_cancelled()
        assert pending.info().state is JobState.CANCELLED
    finally:
        release.set()
        registry.close_all(timeout=5)


def test_delete_long_history_among_many_sessions_keeps_branch(tmp_path, monkeypatch):
    from backend.domain.runtime_state import NodeWriter

    state = WebAppState(tmp_path / "long-history")
    store = state.session_store
    sid = store.create_session("long history").session_id
    store.create_sidebar_thread(session_id=sid, thread_id=sid, title="source")
    store.create_sidebar_thread(session_id=sid, thread_id="sibling", title="sibling")
    for index in range(100):
        store.create_session(f"unrelated-{index}")
    writer = NodeWriter(store)
    parent = store.ensure_root_node(sid, id="root")
    for index in range(250):
        parent = writer.create(
            session_id=sid, thread_id=sid, id=f"turn-{index}", parent=parent, user_content="history " * 512
        )
        parent = writer.finalize(parent, "success")
    monkeypatch.setattr(store, "list_sessions", lambda **_: pytest.fail("delete scanned sessions"))
    monkeypatch.setattr(store, "_objects", lambda *_: pytest.fail("delete loaded complete history"))
    monkeypatch.setattr(store, "sidebar_thread_summary", lambda *_: pytest.fail("delete rebuilt summary"))
    try:
        with TestClient(create_app(state)) as client:
            started = time.monotonic()
            response = client.delete(f"/api/sidebar-threads/{sid}", params={"session_id": sid})
            print(f"delete 250 Turns / 101 sessions HTTP seconds: {time.monotonic() - started:.3f}")
            assert response.status_code == 204, response.text
            assert store.get_sidebar_thread("sibling", session_id=sid).deleted_at is None
            assert store.get_node(sid, "turn-0") is not None
            assert store.get_node(sid, "turn-249") is not None
    finally:
        state.close()


def test_granted_permit_survives_slow_launch_preparation():
    now = [0.0]
    manager = AggregateResources(clock=lambda: now[0], start=False)
    try:
        assert manager.acquire("owner", "starting")["granted"]
        now[0] = 120.0
        manager.tick()
        manager.require_permit("owner", "starting")
        assert not manager.acquire("owner", "next")["granted"]
        manager.cancel("owner", "starting")
        assert manager.acquire("owner", "next")["granted"]
    finally:
        manager.close()


def test_deleted_conversation_stays_closed_after_backend_restart(tmp_path):
    state = WebAppState(tmp_path / "restart")
    sid = state.session_store.create_session("deleted").session_id
    state.session_store.create_sidebar_thread(session_id=sid, thread_id=sid, title="deleted")
    with TestClient(create_app(state)) as client:
        assert client.delete(f"/api/sidebar-threads/{sid}", params={"session_id": sid}).status_code == 204
    state.close()
    reopened = WebAppState(tmp_path / "restart")
    try:
        with pytest.raises(QueueItemStateConflict, match="deleted"):
            reopened.conversation_cache.begin(sid, sid, "late-turn")
        with TestClient(create_app(reopened)) as client:
            response = client.post(
                f"/api/sidebar-threads/{sid}/queued-messages",
                json={"id": "b3e19bca-0c31-4d32-8d24-b7b4fe130724", "content": "late"},
            )
            assert response.status_code == 409
    finally:
        reopened.close()


def test_cleanup_reports_original_failure_and_can_retry(monkeypatch):
    from threading import Event

    from backend.jobs import AdmissionPolicy, JobLane, JobRegistry, JobScopeKind, ThreadJob

    registry = JobRegistry()
    scope = (
        registry.root_scope()
        .child(JobScopeKind.SESSION, session_id="session")
        .child(JobScopeKind.THREAD, thread_id="thread")
    )
    done = Event()
    job = ThreadJob("cleanup", lambda: done.wait(5))
    scope.submit(job, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    close = job.close
    failure = OSError(5, "test termination failed")

    def fail(_timeout):
        raise failure

    monkeypatch.setattr(job, "close", fail)
    try:
        with pytest.raises(OSError) as captured:
            registry.close_threads("session", {"thread"})
        assert captured.value is failure
        monkeypatch.setattr(job, "close", close)
        done.set()
        registry.close_threads("session", {"thread"})
        assert registry.active_count(scope=scope) == 0
    finally:
        monkeypatch.setattr(job, "close", close)
        done.set()
        registry.close_all(timeout=5)


def test_runtime_initialization_does_not_overwrite_changed_limits():
    manager = AggregateResources(start=False)
    try:
        old = {"memory_mib": 128, "processes": 20, "handles": 100}
        updated = {"memory_mib": 64, "processes": 10, "handles": 50}
        manager.configure("owner", old, initialize=True)
        manager.configure("owner", updated)
        manager.configure("owner", old, initialize=True)
        assert manager.status("owner")["limits"] == updated
    finally:
        manager.close()


def test_cancel_after_grant_before_process_start_releases_permit(tmp_path, monkeypatch):
    from backend.jobs import JobScope
    from backend.sandbox import FileAccessMode, NetworkMode, ResourceLimits, SandboxExecutionDecision
    from backend.tools.base import ToolInvocationContext
    from backend.tools.command import WorkspaceCommand
    from tests.testing_sandbox import DirectTestSandboxLauncher

    resources = AggregateResources(start=False)
    launcher = DirectTestSandboxLauncher()
    launcher.wait_resources = lambda ticket, **_: resources.acquire("owner", ticket)
    launcher.cancel_resource_wait = lambda ticket: resources.cancel("owner", ticket)
    monkeypatch.setattr(JobScope, "submit", lambda scope, job, **_: job.cancel("cancel before start"))
    decision = SandboxExecutionDecision(
        launcher=launcher,
        workspaces=(tmp_path,),
        session_id="session",
        user_id="local",
        file_mode=FileAccessMode.READ_ONLY,
        network_mode=NetworkMode.NO_NETWORK,
        network_allowlist=(),
        proxy_port=17831,
        limits=ResourceLimits(),
    )
    commands = WorkspaceCommand(tmp_path, terminal_type="cmd")
    try:
        with pytest.raises(InterruptedError, match="cancelled before launch"):
            commands.run_with_context(ToolInvocationContext(sandbox_decision=decision), "echo unused", 0)
        assert resources.status("owner")["queued"] == 0
        assert resources.acquire("owner", "next")["granted"]
    finally:
        commands.close()
        resources.close()
