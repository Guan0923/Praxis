"""Terminal status, error preservation, and exception mapping."""

from __future__ import annotations

import logging

from backend.domain import TracePersistenceError, error_report, safe_error_message
from backend.domain.runtime_state import NodeStatus, RuntimeState, TerminalErrorCategory, terminal_error_payload


class _FinalizationMixin:
    def _mark_persistence_failure(self, error: BaseException) -> None:
        self.persistence_failed = True
        self.closed = True
        self.terminal_error = terminal_error_payload(
            "server",
            safe_error_message(error),
            retryable=False,
            code="item_persistence_failed",
            error_report=error_report(error),
        )
        # Seal only the durable prefix. Never turn an unsuccessful queued write into a success checkpoint.
        try:
            target = self.assistant or self.last_node
            saved = self.store.get_node(target.session_id, target.id) if target is not None else None
            if isinstance(saved, RuntimeState) and saved.status == "running":
                for message in saved.data[saved.current_data_idx]:
                    for item in message.get("content", []):
                        if item.get("status") == "running":
                            item["status"] = "failed"
                saved.status = "failed"
                self.store.finalize_node(saved)
        except Exception:
            logging.getLogger(__name__).error(
                "Unable to seal the interrupted durable Turn; restart recovery is required."
            )

    def finish(
        self,
        status: NodeStatus,
        final_answer: str = "",
        *,
        category: TerminalErrorCategory | None = None,
        code: str = "",
        error_report: object = None,
    ) -> RuntimeState | None:
        with self._runtime_config_lock:
            try:
                self.writer.flush()
                return self._finish(status, final_answer, category=category, code=code, error_report=error_report)
            except TracePersistenceError as exc:
                self._mark_persistence_failure(exc)
                return None

    def _finish(
        self,
        status: NodeStatus,
        final_answer: str = "",
        *,
        category: TerminalErrorCategory | None = None,
        code: str = "",
        error_report: object = None,
    ) -> RuntimeState | None:
        if self.closed:
            return self.last_node
        if status not in {"success", "paused", "failed"}:
            raise ValueError("A Turn can only finish as success, paused, or failed.")
        if self.assistant is None:
            self.start()
        self._ensure_assistant_message()
        self._finish_stream_item(status="success" if status == "success" else "failed")
        if (
            final_answer
            and status == "success"
            and not any(
                item.get("type") == "text"
                for message in self.assistant.selected_messages[self._last_user_message_index() + 1 :]
                if message["role"] == "assistant"
                for item in message["content"]
            )
        ):
            self._append_item({"type": "text", "text": final_answer, "status": "success"})
        self._settle_running_items("success" if status == "success" else "failed")
        self._drain_collaboration_modes(item_finished=True)
        if final_answer and (status == "failed" or (status == "paused" and category != "user")):
            retryable = status == "paused"
            self.terminal_error = terminal_error_payload(
                category or ("user" if retryable else "agent"),
                final_answer,
                retryable=retryable,
                code=code,
                error_report=error_report,
            )
            self._append_item(self.terminal_error)
        try:
            assert self.assistant is not None
            self.last_node = self.writer.finalize(self.assistant, status)
            self.assistant = self.last_node
        except Exception as exc:
            self._mark_persistence_failure(exc)
            return None
        self.closed = True
        return self.last_node

    def preserve_placeholder(self, *, code: str = "runtime_exception") -> RuntimeState | None:
        return self.finish("failed", "", category="agent", code=code)

    def finish_exception(self, error: BaseException) -> RuntimeState | None:
        category = self.abort_category or self._exception_category(error)
        message = safe_error_message(error)
        return self.finish(
            "failed", message, category=category, code=error.__class__.__name__, error_report=error_report(error)
        )
