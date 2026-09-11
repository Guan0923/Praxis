"""Consume reliable user-message commands and admit their Turn executions."""

from __future__ import annotations

import os
import threading

from pydantic import ValidationError

from backend.domain.runtime_state import RuntimeState
from backend.storage.sqlite import SQLiteSessionStore

from .routes.turn_models import TurnExecutionConfig
from .routes.turn_support import _stream_turn


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
            except Exception:
                if claimed is not None:
                    self.state.message_queue.retry(claimed)
                if self._stop.wait(0.1):
                    return

    def _start(self, claimed) -> None:
        envelope = claimed.envelope
        payload = envelope.payload
        store = SQLiteSessionStore(self.state.paths, getattr(self.state, "agent_thread_index", None))
        operation = str(payload.get("operation") or "create")
        if operation not in {"create", "rewind"}:
            self._reject_permanently(claimed)
            return
        existing = store.find_node(envelope.target_id)
        if isinstance(existing, RuntimeState) and any(
            message.get("delivery_id") == envelope.delivery_id for version in existing.data for message in version
        ):
            self.state.message_queue.ack(claimed)
            return
        if operation == "create" and isinstance(existing, RuntimeState):
            self._reject_permanently(claimed)
            return
        config_payload = payload.get("config")
        try:
            config = TurnExecutionConfig.model_validate(config_payload if isinstance(config_payload, dict) else {})
        except ValidationError:
            self._reject_permanently(claimed)
            return
        if operation == "rewind":
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
                prompt=envelope.content,
                source_id=envelope.target_id,
                config=config,
                references=list(envelope.references),
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
            prompt=envelope.content,
            source_id=str(payload.get("parent_id") or "") or None,
            config=config,
            references=list(envelope.references),
            initial_delivery=claimed,
            stream_response=False,
        )

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
