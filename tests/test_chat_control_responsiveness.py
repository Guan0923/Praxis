"""Deterministic admission/interrupt regressions without a model or live service."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from backend.api.agent_thread_stream import AgentThreadEventHub
from backend.api.conversation_cache import ConversationCache, ConversationCacheEntry
from backend.api.pause_control import TurnPauseController
from backend.api.terminal_manager import TerminalManager, TerminalSession
from backend.domain.input_message import InputMessage
from backend.domain.message_queue import QueuedMessage
from backend.storage.memory_message_queue import MemoryMessageQueue
from backend.storage.runtime_event_stream import MemoryRuntimeEventStream
from backend.storage.todo_list import MemoryTodoListStore


def cache_state():
    state = SimpleNamespace(
        message_queue=MemoryMessageQueue(),
        terminal_manager=TerminalManager(),
        runtime_event_stream=MemoryRuntimeEventStream(),
        agent_thread_events=AgentThreadEventHub(),
        todo_store=MemoryTodoListStore(),
        session_store=SimpleNamespace(get_thread_node=lambda *_: None, get_sidebar_thread=lambda *_, **__: None),
    )
    state.conversation_cache = ConversationCache(state)
    state.message_queue.on_change = state.conversation_cache.trim
    return state


def evict_old_terminal(cache):
    cache.touch("session", "old")
    for index in range(5):
        cache.touch("session", str(index))


@pytest.mark.parametrize("action", ["delete", "ack"])
def test_queue_cleanup_does_not_block_nested_admission_or_other_threads(action, monkeypatch):
    state = cache_state()
    cache, queue, manager = state.conversation_cache, state.message_queue, state.terminal_manager
    cleanup_started, finish_cleanup = Event(), Event()
    inside_transaction, finish_transaction = Event(), Event()
    closed = []

    def close(terminal_id):
        cleanup_started.set()
        assert finish_cleanup.wait(5), "test did not release terminal cleanup"
        with manager._lock:
            manager._sessions.pop(terminal_id, None)
        closed.append(terminal_id)

    monkeypatch.setattr(manager, "close", close)
    for index in range(6):
        thread = str(index)
        cache.entries[thread] = ConversationCacheEntry("session", threads={thread})
    manager._sessions["old-terminal"] = SimpleNamespace(thread_id="0")
    queue.create(QueuedMessage("message", "0", InputMessage.from_input("first")))
    claimed = None
    if action == "ack":
        queue.dispatch(
            delivery_id="delivery", message_ids=["message"], session_id="session", thread_id="0", turn_id="turn"
        )
        claimed = queue.claim("turn", "test")
        assert claimed is not None

    def transact():
        with queue.admission_lock:
            if action == "delete":
                queue.delete("0", "message")
            else:
                queue.ack(claimed)
            inside_transaction.set()
            assert finish_transaction.wait(5)

    def unrelated_operations():
        queue.create(QueuedMessage("other", "unrelated", InputMessage.from_input("before")))
        queue.update("unrelated", "other", message=InputMessage.from_input("after"))
        assert queue.list("unrelated")[0].message.text == "after"
        queue.delete("unrelated", "other")
        cache.begin("session", "running", "another-turn")

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            transaction = workers.submit(transact)
            try:
                assert inside_transaction.wait(1), "queue callback blocked the outer admission transaction"
                assert "0" not in cache.entries, "cache eviction must finish inside admission"
                finish_transaction.set()
                transaction.result(timeout=1)
                assert cleanup_started.wait(1)
                workers.submit(unrelated_operations).result(timeout=1)
                assert queue.list("0") == []
                assert not cache.contains("0")
                # Reopening while old cleanup is blocked must not close the new terminal.
                cache.touch("session", "0")
                with manager._lock:
                    manager._sessions["new-terminal"] = SimpleNamespace(thread_id="0")
                for _ in range(100):
                    cache.trim()
            finally:
                finish_transaction.set()
                finish_cleanup.set()
        cache.flush()
        assert closed == ["old-terminal"]
        assert manager.get("new-terminal") is not None
    finally:
        finish_transaction.set()
        finish_cleanup.set()
        cache.close()


def test_explicit_trim_does_not_run_cleanup_under_an_outer_cache_lock(monkeypatch):
    state = cache_state()
    cache, manager = state.conversation_cache, state.terminal_manager
    entered, release = Event(), Event()
    manager._sessions["old-terminal"] = SimpleNamespace(thread_id="0")

    def close(_terminal_id):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "close", close)
    try:
        with cache.lock:
            for index in range(6):
                cache.touch("session", str(index))
        assert entered.wait(1)
        with ThreadPoolExecutor(max_workers=1) as workers:
            try:
                workers.submit(state.message_queue.ping).result(timeout=1)
            finally:
                release.set()
    finally:
        release.set()
        cache.close()


def test_background_cache_cleanup_failure_is_logged(monkeypatch, caplog):
    state = cache_state()
    cache, manager = state.conversation_cache, state.terminal_manager
    manager._sessions["terminal"] = SimpleNamespace(thread_id="old")
    failure = RuntimeError("local terminal cleanup failure")

    def fail(_terminal_id):
        raise failure

    monkeypatch.setattr(manager, "close", fail)
    evict_old_terminal(cache)
    cache.close()
    assert "Conversation terminal cleanup failed" in caplog.text
    assert any(record.exc_info[1] is failure for record in caplog.records if record.exc_info)


def test_cache_close_waits_for_its_single_cleanup_worker(monkeypatch):
    state = cache_state()
    cache, manager = state.conversation_cache, state.terminal_manager
    entered, release, closing = Event(), Event(), Event()
    manager._sessions["terminal"] = SimpleNamespace(thread_id="old")

    def slow_close(_terminal_id):
        entered.set()
        assert release.wait(5)

    def shutdown():
        closing.set()
        cache.close()

    monkeypatch.setattr(manager, "close", slow_close)
    evict_old_terminal(cache)
    try:
        assert entered.wait(1)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(shutdown)
            try:
                assert closing.wait(1)
                assert not future.done()
            finally:
                release.set()
            future.result(timeout=1)
    finally:
        release.set()
        cache.close()


def test_deletion_reports_its_cleanup_failure_and_attempts_other_terminals(monkeypatch, caplog):
    state = cache_state()
    cache, manager = state.conversation_cache, state.terminal_manager
    manager._sessions["failed"] = SimpleNamespace(thread_id="old")
    manager._sessions["other"] = SimpleNamespace(thread_id="old")
    failure = RuntimeError("local deletion cleanup failure")
    attempted = set()

    def close(terminal_id):
        attempted.add(terminal_id)
        if terminal_id == "failed":
            raise failure

    monkeypatch.setattr(manager, "close", close)
    try:
        with pytest.raises(ExceptionGroup, match="terminal cleanup failed") as caught:
            cache.release("old")
        assert caught.value.exceptions == (failure,)
        assert attempted == {"failed", "other"}
        assert "Conversation terminal cleanup failed" in caplog.text
        # An unrelated deletion must not inherit a previous task's failure.
        cache.release("unrelated")
    finally:
        cache.close()


def test_pause_is_accepted_while_steering_abort_is_blocked():
    controller = TurnPauseController()
    entered, release = Event(), Event()
    calls = []

    def abort():
        calls.append("abort")
        entered.set()
        assert release.wait(5)

    unregister = controller.register_abort(abort)
    with ThreadPoolExecutor(max_workers=2) as workers:
        steer = workers.submit(controller.dispatch_steering, lambda: None)
        try:
            assert entered.wait(1)
            assert workers.submit(controller.request_pause).result(timeout=1) is True
            assert controller.is_requested()
            assert not steer.done()
            workers.submit(unregister).result(timeout=1)
            assert calls == ["abort"]
        finally:
            release.set()
        steer.result(timeout=1)


def test_abort_failures_are_visible_without_skipping_other_aborters():
    controller = TurnPauseController()
    observed = []
    failure = RuntimeError("local abort failure")

    def fail():
        raise failure

    controller.register_abort(fail)
    controller.register_abort(lambda: observed.append("aborted"))
    with pytest.raises(ExceptionGroup, match="interruption failed") as caught:
        controller.dispatch_steering(lambda: None)
    assert caught.value.exceptions == (failure,)
    assert observed == ["aborted"]
    assert controller.request_pause()
    assert controller.is_requested()


def test_each_new_operation_can_register_an_abort_after_steering_is_consumed():
    controller = TurnPauseController()
    calls = []

    def first():
        calls.append("first")

    unregister = controller.register_abort(first)
    assert controller.request_steering()
    assert controller.take_steering(lambda: ["message"]) == ["message"]
    unregister()
    controller.register_abort(lambda: calls.append("second"))
    assert controller.request_pause()
    assert calls == ["first", "second"]


def test_pause_wins_before_steering_dispatch():
    controller = TurnPauseController()
    assert controller.request_pause()
    dispatched = []
    with pytest.raises(ValueError, match="paused"):
        controller.dispatch_steering(lambda: dispatched.append(True))
    assert dispatched == []


def test_real_terminal_close_does_not_close_a_reopened_terminal():
    state = cache_state()
    cache, manager = state.conversation_cache, state.terminal_manager
    joining, release = Event(), Event()

    class Reader:
        def join(self, timeout):
            joining.set()
            assert release.wait(timeout)

    # A stopped process requires no OS termination; the real manager still
    # removes the old session, joins its reader and releases its output.
    process = SimpleNamespace(isalive=lambda: False)
    old = TerminalSession("old-terminal", "cmd", "test", process, manager.output, thread_id="old")
    old.reader_thread = Reader()
    manager._sessions[old.id] = old
    new = TerminalSession("new-terminal", "cmd", "test", process, manager.output, thread_id="old")
    try:
        evict_old_terminal(cache)
        assert joining.wait(1)
        assert old.closed and manager.get(old.id) is None
        cache.touch("session", "old")
        with manager._lock:
            manager._sessions[new.id] = new
        with ThreadPoolExecutor(max_workers=1) as workers:
            try:
                workers.submit(state.message_queue.ping).result(timeout=1)
            finally:
                release.set()
        cache.flush()
        assert manager.get(new.id) is new
        assert not new.closed
    finally:
        release.set()
        cache.close()
        manager.close_all()


def test_deletion_release_waits_without_holding_admission(monkeypatch):
    state = cache_state()
    cache, manager = state.conversation_cache, state.terminal_manager
    entered, release = Event(), Event()
    manager._sessions["terminal"] = SimpleNamespace(thread_id="old")

    def close(_terminal_id):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "close", close)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            deletion = workers.submit(cache.release, "old")
            try:
                assert entered.wait(1)
                assert not deletion.done()
                workers.submit(state.message_queue.ping).result(timeout=1)
            finally:
                release.set()
            deletion.result(timeout=1)
    finally:
        release.set()
        cache.close()
