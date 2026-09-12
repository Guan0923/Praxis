"""Consume reliable user-message commands and admit their Turn executions."""

from __future__ import annotations

import os
import threading

from backend.domain import error_report, safe_error_message
from backend.domain.runtime_state import RuntimeState
from backend.storage.sqlite import SQLiteSessionStore

from .routes.turn_support import _stream_turn, fail_initial_turn


class TurnMessageWorker:
    def __init__(self, state) -> None:
        self.state = state
        self.consumer = f"turn-start-{os.getpid()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="turn-message-worker", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self.state.message_queue.wake()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            claimed = None
            try:
                claimed = self.state.message_queue.wait_turn_start(self.consumer, self._stop)
                if claimed is None:
                    return
                self._start(claimed)
            except Exception as exc:
                if claimed is not None:
                    start = claimed.envelope.start
                    if start is not None and start.operation == "create":
                        self._fail(claimed, exc)
                    else:
                        self.state.message_queue.retry(claimed)

    def _start(self, claimed) -> None:
        envelope = claimed.envelope
        start = envelope.start
        store = SQLiteSessionStore(self.state.paths, getattr(self.state, "agent_thread_index", None))
        operation = start.operation if start is not None else None
        if operation not in {"create", "rewind"}:
            self._reject_permanently(claimed)
            return
        config = start.config
        if operation == "rewind":
            existing = store.get_node(envelope.session_id, envelope.target_id)
            if (
                not isinstance(existing, RuntimeState)
                or existing.session_id != envelope.session_id
                or existing.thread_id != envelope.thread_id
            ):
                self._reject_permanently(claimed)
                return
            _stream_turn(
                self.state,
                session_id=envelope.session_id,
                thread_id=envelope.thread_id,
                turn_id=envelope.target_id,
                message=envelope.message,
                source_id=envelope.target_id,
                config=config,
                adopt_existing=True,
                initial_delivery=claimed,
                stream_response=False,
            )
            return
        _stream_turn(
            self.state,
            session_id=envelope.session_id,
            thread_id=envelope.thread_id,
            turn_id=envelope.target_id,
            message=envelope.message,
            source_id=envelope.target_id,
            config=config,
            adopt_existing=True,
            precreated=True,
            initial_delivery=claimed,
            stream_response=False,
        )

    def _fail(self, claimed, exc: Exception) -> None:
        from .runtime_event_transport import publish_terminal

        envelope = claimed.envelope
        try:
            store = SQLiteSessionStore(self.state.paths, getattr(self.state, "agent_thread_index", None))
            node = store.get_node(envelope.session_id, envelope.target_id)
            if isinstance(node, RuntimeState) and node.status == "running":
                fail_initial_turn(store, node, exc)
        finally:
            try:
                publish_terminal(
                    self.state,
                    session_id=envelope.session_id,
                    thread_id=envelope.thread_id,
                    turn_id=envelope.target_id,
                    terminal_type="failed",
                    message=safe_error_message(exc),
                    error_report=error_report(exc),
                )
            finally:
                self.state.message_queue.ack(claimed)

    def _reject_permanently(self, claimed) -> None:
        from .runtime_event_transport import publish_terminal

        envelope = claimed.envelope
        publish_terminal(
            self.state,
            session_id=envelope.session_id,
            thread_id=envelope.thread_id,
            turn_id=envelope.target_id,
            terminal_type="failed",
            message="Turn 启动请求无效，已停止重试。",
        )
        self.state.message_queue.ack(claimed)


__all__ = ["TurnMessageWorker"]
