"""Durable Assistant-report dispatch and runtime report consumption."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.domain import (
    AssistantMessage,
    MessageEnvelope,
    ThreadNode,
)
from backend.domain.execution_config import TurnExecutionConfig
from backend.domain.file_paths import FILE_SOURCES, ScopedPaths
from backend.domain.input_message import InputMessage
from backend.domain.runtime_state import (
    NodeFrame,
    RuntimeState,
)
from backend.tools import ToolError

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent


def _notify_all(callbacks: list[Callable[[], None]] | tuple[Callable[[], None], ...]) -> None:
    errors: list[Exception] = []
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:
            errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("Agent report notification failures", errors)


class _SubagentReportDeliveryMixin:
    """Deliver one canonical child report to each registered recipient."""

    def _publish_turn_reports(self, node: ThreadNode, turn: RuntimeState) -> None:
        if turn.status not in {"success", "failed"}:
            return
        session_id, turn_id, thread_status = node.session_id, turn.id, turn.status
        reply_content = self._reply_content(node, turn)
        notifications: list[Callable[[], None]] = []
        with self._queue.prepare_reports() as stage:
            if self._closed:
                raise RuntimeError("Agent coordinator is closing.")
            with self._store.prepare_agent_turn_reports(
                session_id, turn_id, thread_status=thread_status, reply_content=reply_content
            ) as reports:
                for report in reports:
                    envelope = MessageEnvelope(
                        delivery_id=report.delivery_id,
                        sender_kind="agent",
                        source_thread_id=report.agent_thread_id,
                        target_kind="report",
                        target_id=report.recipient_thread_id,
                        session_id=report.session_id,
                        thread_id=report.recipient_thread_id,
                        message=InputMessage(report.reply_content),
                        report_status=report.thread_status,
                        source_message_ids=(report.delivery_id,),
                        created_at=report.created_at,
                        correlation_id=report.turn_id,
                    )
                    stage(envelope)
                    notifications.append(self._prepare_report_notification(session_id, envelope.thread_id))
        # SQLite has committed and the queue is visible. Execution failures cannot undo delivery.
        _notify_all(notifications)

    def _prepare_report_notification(self, session_id: str, thread_id: str) -> Callable[[], None]:
        self._queue.require_thread_open(thread_id)
        return lambda: self.receive_pending_reports(session_id, thread_id)

    def receive_pending_reports(self, session_id: str, thread_id: str) -> None:
        with self._queue.admission_lock:
            if self._closed or not self._queue.has_reports(thread_id):
                return
            active = self._report_receivers.get((session_id, thread_id))
            if active is None:
                self._drain_inactive_reports(session_id, thread_id)
                return
            runtime, aborters = active
            if runtime.stop_requested():
                return
            callbacks = tuple(aborters)
        _notify_all(callbacks)

    @contextmanager
    def report_receiver(self, runtime: AgentRuntime) -> Iterator[None]:
        """Bind operation aborts to the recipient, without cancelling its task."""
        if self._queue is None:
            yield
            return
        key = (runtime.state.session_id, str(runtime.run.thread_id or runtime.state.session_id))
        aborters: set[Callable[[], None]] = set()
        previous_register = runtime.services.register_operation_abort
        previous_interrupted = runtime.services.operation_interrupted
        previous_report_pending = runtime.services.agent_report_pending

        def interrupted() -> bool:
            return bool((previous_interrupted and previous_interrupted()) or self._queue.has_reports(key[1]))

        def register(abort: Callable[[], None]) -> Callable[[], None]:
            remove_previous = previous_register(abort) if previous_register else None
            with self._queue.admission_lock:
                aborters.add(abort)
                abort_now = self._queue.has_reports(key[1])
            if abort_now:
                abort()

            def unregister() -> None:
                with self._queue.admission_lock:
                    aborters.discard(abort)
                if remove_previous:
                    remove_previous()

            return unregister

        with self._queue.admission_lock:
            self._queue.ping()
            self._queue.require_thread_open(key[1])
            if self._closed:
                raise RuntimeError("Agent coordinator is closing.")
            if key in self._report_receivers:
                raise RuntimeError("The report recipient already has a running task.")
            self._report_receivers[key] = (runtime, aborters)
            runtime.services.register_operation_abort = register
            runtime.services.operation_interrupted = interrupted
            runtime.services.agent_report_pending = lambda: self._queue.has_reports(key[1])
        try:
            yield
        finally:
            with self._queue.admission_lock:
                self._report_receivers.pop(key, None)
                runtime.services.register_operation_abort = previous_register
                runtime.services.operation_interrupted = previous_interrupted
                runtime.services.agent_report_pending = previous_report_pending

    def _drain_inactive_reports(self, session_id: str, thread_id: str) -> None:
        if self._queue.has_start_request(thread_id):
            return
        runtime_thread = self._store.get_runtime_thread(session_id, thread_id)
        if runtime_thread is None or runtime_thread.running_turn_id:
            return
        turn_id = runtime_thread.current_turn_id
        turn = self._store.get_node(session_id, turn_id) if turn_id else None
        target = self._store.get_thread_node(session_id, thread_id)
        if not isinstance(turn, RuntimeState) or target is None:
            return
        was_paused = turn.status == "paused"
        if turn.status not in {"paused", "success", "failed"}:
            return
        first_sender = ""
        while True:
            claimed = self._queue.claim_report(thread_id, f"agent-report-inactive-{thread_id}")
            if claimed is None:
                break
            envelope = claimed.envelope
            first_sender = first_sender or envelope.source_thread_id
            turn = self._store.append_agent_report(
                session_id, thread_id, delivery_id=envelope.delivery_id, reply_content=envelope.message.text
            )
            if self._thread_events is not None:
                self._thread_events.publish_frame(thread_id, NodeFrame.snapshot(turn), turn)
            self._queue.ack(claimed)
        if was_paused and first_sender and not self._closed:
            current = self._store.get_node(session_id, turn.id)
            if isinstance(current, RuntimeState) and current.status == "paused":
                resumed = self._store.resume_turn_node(current.id)
                self._submit_turn(target, resumed, creator_thread_id=first_sender)

    def consume_runtime_reports(self, runtime: AgentRuntime) -> int:
        """Commit history and processing status before context updates and ACK."""
        if self._store is None or self._queue is None:
            return 0
        thread_id = str(runtime.run.thread_id or runtime.state.session_id)
        if runtime.run.status != "running" or not runtime.run.turn_id or runtime.stop_requested():
            return 0
        count = 0
        with self._queue.admission_lock:
            if self._closed:
                return 0
            while True:
                claimed = self._queue.claim_report(thread_id, f"agent-report-running-{thread_id}")
                if claimed is None:
                    break
                envelope = claimed.envelope
                if runtime.services.persist_agent_report is not None:
                    runtime.services.persist_agent_report(envelope.delivery_id, envelope.message.text)
                else:
                    self._store.append_agent_report(
                        runtime.state.session_id,
                        thread_id,
                        delivery_id=envelope.delivery_id,
                        reply_content=envelope.message.text,
                    )
                runtime.state.messages.append(AssistantMessage(name="subagent_report", content=envelope.message.text))
                runtime.run.history = runtime.state.messages
                publish = runtime.services.publish or (lambda _event: None)
                publish(
                    RuntimeEvent(
                        "subagent_report",
                        envelope.message.text,
                        {
                            "reply_content": envelope.message.text,
                            "delivery_id": envelope.delivery_id,
                            "report_status": envelope.report_status,
                        },
                    )
                )
                self._queue.ack(claimed)
                count += 1
        if count:
            runtime.save()
        return count

    def send_from_root(
        self,
        session_id: str,
        target_thread_id: str,
        content: str,
        *,
        references: list[dict[str, str]] | None = None,
        runtime_config: TurnExecutionConfig | None = None,
    ) -> dict[str, object]:
        self._require_services()
        target = getattr(self._store, "get_thread_node")(session_id, target_thread_id)
        source = (
            getattr(self._store, "get_thread_node")(session_id, target.root_thread_id) if target is not None else None
        )
        if (
            source is None
            or source.depth != 0
            or source.root_thread_id != source.thread_id
            or target is None
            or target.session_id != session_id
            or target.root_thread_id != source.thread_id
            or target.depth <= 0
        ):
            raise ToolError("The target must be a Subagent Thread in this Session tree.")
        parsed_references = self._references_from_web(session_id, target, references or [])
        if not content.strip():
            raise ToolError("Agent message requires task text.")
        return self._dispatch_message(
            session_id,
            source.thread_id,
            target_thread_id,
            content,
            references=parsed_references,
            runtime_config=runtime_config,
        )

    def _references_from_web(
        self,
        session_id: str,
        target: ThreadNode,
        values: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        if not values:
            return []
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, target.thread_id)
        turn = (
            getattr(self._store, "get_node")(session_id, runtime_thread.current_turn_id)
            if runtime_thread is not None and runtime_thread.current_turn_id
            else None
        )
        if not isinstance(turn, RuntimeState):
            raise ToolError("The target Agent has no canonical current Turn.")
        paths = ScopedPaths(Path(turn.cwd), Path(turn.project_cwd) if turn.project_cwd else None)
        references: list[dict[str, str]] = []
        for value in values:
            source, raw_path, display_path = value.get("source"), value.get("path"), value.get("display_path")
            if (
                source not in FILE_SOURCES
                or not isinstance(raw_path, str)
                or not isinstance(display_path, str)
                or not display_path
            ):
                raise ToolError("Browser Agent references require source, path, and display_path.")
            scope = "project" if source == "project" else "workspace"
            try:
                prefix = raw_path.partition(":")[0]
                if prefix in {"workspace", "project"} and prefix != scope:
                    raise ValueError("File reference source does not match its path prefix.")
                resolved = paths.resolve(raw_path)
                scoped = paths.format(resolved, scope=scope)
                if source == "upload" and not resolved.is_relative_to(paths.workspace / "uploads"):
                    raise ValueError("Upload references must stay inside workspace:uploads/.")
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            if not resolved.is_file():
                raise ToolError(f"Referenced path is not a file: {scoped}")
            references.append({"source": str(source), "path": scoped, "display_path": scoped})
        return references
