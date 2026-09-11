"""Single-shot report handoff against real SQLite and local model HTTP."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import sleep

import pytest

from backend.configuration import ClientPaths
from backend.domain import AssistantMessage, MessageEnvelope, ToolMessage
from backend.domain.execution_config import TurnExecutionConfig
from backend.domain.input_message import InputMessage
from backend.domain.message_queue import TurnStart
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.subagents import SubagentCoordinator
from backend.storage.message_queue import MemoryMessageQueue
from backend.storage.sqlite import SQLiteSessionStore
from backend.tools import Tool, ToolRegistry
from tests.test_agent_threads import _agent_create, _finished_source
from tests.test_incomplete_responses import canonical_runner, stream_events, wire_response


def report_fixture(tmp_path, *, running=False):
    store = SQLiteSessionStore(ClientPaths(tmp_path / "reports"))
    session = store.create_session("report handoff")
    parent = _finished_source(store, session.session_id)
    if running:
        with store._connection(session.session_id, write=True) as connection:
            connection.execute(
                "UPDATE runtime_threads SET running_turn_id=? WHERE thread_id=?", (parent.id, parent.thread_id)
            )
    child = _agent_create(session.session_id, parent, name="worker")
    store.create_agent_thread(session.session_id, child)
    store.register_agent_turn_report(session.session_id, child.turn.id, child.node.thread_id, parent.thread_id)
    finished = child.turn.clone()
    finished.status = "success"
    store.finalize_node(finished)
    queue = MemoryMessageQueue()
    coordinator = SubagentCoordinator(store=store, message_queue=queue)
    return store, parent, child.node, finished, queue, coordinator


@pytest.mark.parametrize("failure", ["write", "enqueue", "notification", "commit"])
def test_preparation_failure_rolls_back_disk_and_queue(tmp_path, monkeypatch, failure):
    store, parent, child, turn, queue, coordinator = report_fixture(tmp_path)
    original_prepare = queue.prepare_reports
    observed = []

    @contextmanager
    def prepare():
        with original_prepare() as stage:

            def checked(envelope):
                stage(envelope)
                observed.append(envelope)
                assert queue.claim_report(parent.thread_id, "early") is None
                if failure == "enqueue":
                    raise RuntimeError("enqueue failed")

            yield checked

    monkeypatch.setattr(queue, "prepare_reports", prepare)
    if failure == "notification":

        def fail_notification(*_args):
            raise RuntimeError("notification failed")

        monkeypatch.setattr(coordinator, "_prepare_report_notification", fail_notification)
    if failure == "write":
        with store._connection(parent.session_id, write=True) as connection:
            connection.execute(
                "CREATE TRIGGER fail_report BEFORE UPDATE ON agent_turn_reports BEGIN SELECT RAISE(ABORT, 'write failed'); END"
            )
    if failure == "commit":
        original_connect = sqlite3.connect

        class FailingCommit(sqlite3.Connection):
            def commit(self):
                if self.total_changes and observed:
                    raise sqlite3.OperationalError("commit failed")
                super().commit()

        monkeypatch.setattr(
            sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, **kwargs, factory=FailingCommit)
        )
    try:
        with pytest.raises((RuntimeError, sqlite3.Error), match=failure + " failed"):
            coordinator._publish_turn_reports(child, turn)
        reports = store.list_agent_turn_reports(parent.session_id)
        assert reports[0].state == "waiting"
        assert reports[0].thread_status is None
        assert reports[0].reply_content == ""
        assert not queue.has_reports(parent.thread_id)
        assert queue.claim_report(parent.thread_id, "after") is None
        saved = store.get_node(parent.session_id, parent.id)
        assert len(saved.data[saved.current_data_idx]) == 2
    finally:
        coordinator.close()


def test_unread_report_is_queued_once_without_idle_database_queries(tmp_path, monkeypatch):
    store, parent, child, turn, queue, coordinator = report_fixture(tmp_path, running=True)

    def no_scan(*_args, **_kwargs):
        pytest.fail("report delivery scanned the database")

    monkeypatch.setattr(store, "list_agent_turn_reports", no_scan)
    coordinator.bind_session(parent.session_id, lambda: None, tmp_path)
    try:
        coordinator._publish_turn_reports(child, turn)
        sleep(1.2)
        assert len(queue._report_streams[parent.thread_id]) == 1
        assert not queue._receipts
        with pytest.raises(ValueError, match="already been sent"):
            coordinator._publish_turn_reports(child, turn)
        assert len(queue._report_streams[parent.thread_id]) == 1
    finally:
        coordinator.close()


def test_history_and_delivered_state_roll_back_together(tmp_path):
    store, parent, child, turn, queue, coordinator = report_fixture(tmp_path, running=True)
    try:
        coordinator._publish_turn_reports(child, turn)
        report = store.list_agent_turn_reports(parent.session_id)[0]
        with store._connection(parent.session_id, write=True) as connection:
            connection.execute(
                "CREATE TRIGGER fail_ack BEFORE UPDATE ON agent_turn_reports WHEN NEW.state='delivered' BEGIN SELECT RAISE(ABORT, 'consume failed'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="consume failed"):
            store.append_agent_report(
                parent.session_id, parent.thread_id, delivery_id=report.delivery_id, reply_content=report.reply_content
            )
        assert store.list_agent_turn_reports(parent.session_id)[0].state == "queued"
        saved = store.get_node(parent.session_id, parent.id)
        assert len(saved.data[saved.current_data_idx]) == 2
    finally:
        coordinator.close()


@pytest.mark.parametrize("closed", [False, True])
def test_deleted_or_closed_recipient_cannot_be_notified(tmp_path, closed):
    store, parent, child, turn, queue, coordinator = report_fixture(tmp_path)
    if closed:
        coordinator.close()
    else:
        queue.discard_threads({parent.thread_id})
    try:
        with pytest.raises((RuntimeError, ValueError)):
            coordinator._publish_turn_reports(child, turn)
        assert store.list_agent_turn_reports(parent.session_id)[0].state == "waiting"
        assert not queue.has_reports(parent.thread_id)
    finally:
        coordinator.close()


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_real_model_request_is_interrupted_and_partial_tool_is_not_executed(tmp_path, mode):
    started, release = Event(), Event()
    requests, executed = [], []

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if len(requests) == 1:
                event = {
                    "id": "partial",
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "old-call",
                                        "type": "function",
                                        "function": {"name": "local_operation", "arguments": '{"value": "unfinished'},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
                started.set()
                release.wait(8)
                return
            response = wire_response("chat_completions", text="continued with report")
            events = stream_events("chat_completions", response)
            self.wfile.write(
                ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()
            )
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    tools = ToolRegistry(
        [Tool("local_operation", "Local test operation", lambda value: executed.append(value), read_only=True)]
    )
    try:
        with canonical_runner(
            tmp_path, "chat_completions", f"http://127.0.0.1:{server.server_port}/v1", tools, mode=mode
        ) as (runner, runtime, bridge, store, _frames, _events):
            parent = store.get_node(runtime.state.session_id, runtime.run.turn_id)
            child = _agent_create(parent.session_id, parent, name="reporter")
            store.create_agent_thread(parent.session_id, child)
            store.register_agent_turn_report(parent.session_id, child.turn.id, child.node.thread_id, parent.thread_id)
            finished = child.turn.clone()
            finished.status = "success"
            store.finalize_node(finished)
            queue = MemoryMessageQueue()
            coordinator = SubagentCoordinator(store=store, message_queue=queue)
            runner.subagents = coordinator
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(runner.run, runtime)
                try:
                    assert started.wait(5), "local model was not called"
                    coordinator._publish_turn_reports(child.node, finished)
                    result = future.result(timeout=5)
                    assert result.status == "completed"
                    assert len(requests) == 2
                    assert "thread_path: /root/reporter" in json.dumps(requests[1])
                    assert "unfinished" not in json.dumps(requests[1])
                    assert executed == []
                    assert store.list_agent_turn_reports(parent.session_id)[0].state == "delivered"
                finally:
                    release.set()
                    coordinator.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(3)


def test_queued_start_keeps_reports_for_the_incoming_task(tmp_path):
    store, parent, child, turn, queue, coordinator = report_fixture(tmp_path)
    queue.dispatch_turn_start(
        MessageEnvelope(
            delivery_id="queued-start",
            sender_kind="user",
            source_thread_id=parent.thread_id,
            target_kind="turn_start",
            target_id="next-turn",
            session_id=parent.session_id,
            thread_id=parent.thread_id,
            message=InputMessage("next"),
            source_message_ids=("queued-start",),
            start=TurnStart("create", TurnExecutionConfig()),
        )
    )
    try:
        coordinator._publish_turn_reports(child, turn)
        assert store.list_agent_turn_reports(parent.session_id)[0].state == "queued"
        assert len(store.get_node(parent.session_id, parent.id).data[0]) == 2
        queue.ack(queue.claim_turn_start("startup"))
        coordinator.receive_pending_reports(parent.session_id, parent.thread_id)
        assert store.list_agent_turn_reports(parent.session_id)[0].state == "delivered"
    finally:
        coordinator.close()


def attach_reporter(store, runtime, runner):
    parent = store.get_node(runtime.state.session_id, runtime.run.turn_id)
    child = _agent_create(parent.session_id, parent, name="reporter")
    store.create_agent_thread(parent.session_id, child)
    store.register_agent_turn_report(parent.session_id, child.turn.id, child.node.thread_id, parent.thread_id)
    finished = child.turn.clone()
    finished.status = "success"
    store.finalize_node(finished)
    coordinator = SubagentCoordinator(store=store, message_queue=MemoryMessageQueue())
    runner.subagents = coordinator
    return coordinator, child.node, finished


@pytest.mark.parametrize("interruptible", [False, True])
def test_real_tool_preserves_finished_results_and_stops_remaining_calls(tmp_path, interruptible):
    started, release = Event(), Event()
    processes = []
    completed_file = tmp_path / "completed.txt"
    late_file = tmp_path / "late.txt"

    def done():
        completed_file.write_text("done", encoding="utf-8")
        return "done"

    def late():
        late_file.write_text("unexpected", encoding="utf-8")
        return "unexpected"

    def slow(context):
        if not interruptible:
            started.set()
            if not release.wait(5):
                raise RuntimeError("test did not release the operation")
            return "finished safely"
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        remove = context.register_abort(process.terminate)
        try:
            assert process.stdout.readline().strip() == "ready"
            started.set()
            process.communicate(timeout=5)
            return "interrupted owned child"
        finally:
            remove()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def approve_late(_request):
        if not release.wait(5):
            raise RuntimeError("test did not release the queued operation")
        return InterruptDecision("approve")

    tools = ToolRegistry(
        [
            Tool("done", "done", done),
            Tool("slow", "slow", lambda: "", context_handler=slow),
            Tool("late", "late", late, requires_confirmation=True),
        ]
    )

    class Planner:
        name = "local-report-tools"
        calls = 0

        def decide(self, runtime):
            self.calls += 1
            if self.calls == 1:
                return AssistantMessage(tool_messages=[ToolMessage(name="done", call_id="done", arguments={})])
            if self.calls == 2:
                return AssistantMessage(
                    tool_messages=[ToolMessage(name=name, call_id=name, arguments={}) for name in ("slow", "late")]
                )
            assert any(
                message.name == "subagent_report"
                for message in runtime.state.messages
                if isinstance(message, AssistantMessage)
            )
            return AssistantMessage(content="continued")

    with canonical_runner(tmp_path, "chat_completions", "http://127.0.0.1:1/v1", tools) as (
        runner,
        runtime,
        _bridge,
        store,
        _frames,
        _events,
    ):
        runner.planner = Planner()
        runtime.services.interrupt = approve_late
        runner.settings = replace(runner.settings, max_tool_parellel=1)
        coordinator, child, finished = attach_reporter(store, runtime, runner)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runner.run, runtime)
            try:
                assert started.wait(5)
                coordinator._publish_turn_reports(child, finished)
                if not interruptible:
                    assert not future.done()
                release.set()
                assert future.result(timeout=5).status == "completed"
                assert completed_file.read_text() == "done"
                assert not late_file.exists()
                assert all(process.poll() is not None for process in processes)
                assert runtime.run.actions[0].content == "done"
                assert runtime.run.actions[1].content == (
                    "interrupted owned child" if interruptible else "finished safely"
                )
            finally:
                release.set()
                coordinator.close()


def test_cancelling_recipient_is_not_interrupted_or_restarted_by_a_report(tmp_path):
    tools = ToolRegistry([Tool("unused", "unused", lambda: "unused")])
    with canonical_runner(tmp_path, "chat_completions", "http://127.0.0.1:1/v1", tools) as (
        runner,
        runtime,
        _bridge,
        store,
        _frames,
        _events,
    ):
        coordinator, child, finished = attach_reporter(store, runtime, runner)
        aborted = Event()
        runtime.services.cancel_requested = lambda: True
        try:
            with coordinator.report_receiver(runtime):
                unregister = runtime.services.register_operation_abort(aborted.set)
                coordinator._publish_turn_reports(child, finished)
                assert not aborted.is_set()
                assert coordinator.consume_runtime_reports(runtime) == 0
                assert not coordinator._jobs
                unregister()
            assert store.list_agent_turn_reports(runtime.state.session_id)[0].state == "queued"
        finally:
            coordinator.close()


def test_new_report_during_consumption_is_not_lost_or_reordered(tmp_path):
    tools = ToolRegistry([Tool("unused", "unused", lambda: "unused")])
    with canonical_runner(tmp_path, "chat_completions", "http://127.0.0.1:1/v1", tools) as (
        runner,
        runtime,
        _bridge,
        store,
        _frames,
        _events,
    ):
        coordinator, first, first_turn = attach_reporter(store, runtime, runner)
        parent = store.get_node(runtime.state.session_id, runtime.run.turn_id)
        second = _agent_create(parent.session_id, parent, name="second")
        store.create_agent_thread(parent.session_id, second)
        store.register_agent_turn_report(parent.session_id, second.turn.id, second.node.thread_id, parent.thread_id)
        second_turn = second.turn.clone()
        second_turn.status = "success"
        store.finalize_node(second_turn)
        published = []

        def publish(event):
            published.append(event.message)
            if len(published) == 1:
                coordinator._publish_turn_reports(second.node, second_turn)

        runtime.services.publish = publish
        try:
            with coordinator.report_receiver(runtime):
                coordinator._publish_turn_reports(first, first_turn)
                assert coordinator.consume_runtime_reports(runtime) == 2
                assert coordinator.consume_runtime_reports(runtime) == 0
            assert "thread_path: /root/reporter" in published[0]
            assert "thread_path: /root/second" in published[1]
            assert len(store.list_agent_turn_reports(parent.session_id, states=("delivered",))) == 2
            assert not coordinator._queue.has_reports(parent.thread_id)
        finally:
            coordinator.close()


def test_report_does_not_approve_or_execute_an_obsolete_approval(tmp_path):
    requested, release = Event(), Event()
    executed = []

    def approve(_request):
        requested.set()
        if not release.wait(5):
            raise RuntimeError("approval test was not released")
        return InterruptDecision("approve")

    def operation():
        executed.append(True)
        return "executed"

    tools = ToolRegistry([Tool("needs_approval", "approval", operation, requires_confirmation=True)])

    class Planner:
        name = "local-approval"
        calls = 0

        def decide(self, _runtime):
            self.calls += 1
            if self.calls == 1:
                return AssistantMessage(
                    tool_messages=[ToolMessage(name="needs_approval", call_id="approval", arguments={})]
                )
            return AssistantMessage(content="received report")

    with canonical_runner(tmp_path, "chat_completions", "http://127.0.0.1:1/v1", tools, interrupt=approve) as (
        runner,
        runtime,
        _bridge,
        store,
        _frames,
        _events,
    ):
        runner.planner = Planner()
        coordinator, child, finished = attach_reporter(store, runtime, runner)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runner.run, runtime)
            try:
                assert requested.wait(5)
                coordinator._publish_turn_reports(child, finished)
                assert not future.done()
                assert not executed
                release.set()
                assert future.result(timeout=5).status == "completed"
                assert not executed
            finally:
                release.set()
                coordinator.close()
