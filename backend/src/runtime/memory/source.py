"""Read one visible sidebar conversation as a Memory source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from backend.domain.runtime_state import RuntimeRootState, RuntimeState, RuntimeStateTree

from .extraction import MemorySessionSnapshot, MemorySourceMessage


@dataclass(frozen=True)
class MemoryConversation:
    session_id: str
    thread_id: str
    project_id: str | None
    updated_at: datetime
    running: bool
    archived: bool
    deleted: bool


class MemoryConversationSource:
    def __init__(self, sessions, projects) -> None:
        self.sessions = sessions
        self.projects = projects

    def list(self) -> list[MemoryConversation]:
        result: list[MemoryConversation] = []
        for summary in self.sessions.list_sidebar_thread_summaries(state="all"):
            thread_item = summary.thread
            project = self.projects.session_project(thread_item.session_id)
            project_id = project.project_id if project is not None and project.removed_at is None else None
            runtime = self.sessions.get_runtime_thread(thread_item.session_id, thread_item.thread_id)
            if runtime is None or runtime.origin_kind not in {"main", "fork"}:
                continue
            result.append(
                MemoryConversation(
                    thread_item.session_id,
                    thread_item.thread_id,
                    project_id,
                    datetime.fromisoformat(thread_item.last_activity_at).astimezone(UTC),
                    runtime.running_turn_id is not None,
                    thread_item.archived_at is not None,
                    thread_item.deleted_at is not None,
                )
            )
        return result

    def get(self, thread_id: str) -> MemoryConversation | None:
        return next((item for item in self.list() if item.thread_id == thread_id), None)

    def snapshot(self, conversation: MemoryConversation) -> MemorySessionSnapshot:
        thread = self.sessions.get_runtime_thread(conversation.session_id, conversation.thread_id)
        if thread is None or thread.current_turn_id is None:
            return MemorySessionSnapshot(conversation.thread_id, (), "idle", conversation.project_id)
        node = self.sessions.get_node(conversation.session_id, thread.current_turn_id)
        if not isinstance(node, RuntimeState):
            return MemorySessionSnapshot(conversation.thread_id, (), "idle", conversation.project_id)
        path = RuntimeStateTree(self.sessions.load_nodes(conversation.session_id)).ancestors(node)
        messages: list[MemorySourceMessage] = []
        position = 0
        for turn in path:
            if isinstance(turn, RuntimeRootState):
                continue
            for message in turn.selected_messages:
                role = str(message.get("role") or "")
                if role not in {"user", "assistant"}:
                    continue
                text = "".join(
                    str(item.get("text") or item.get("message") or "")
                    for item in message.get("content", [])
                    if isinstance(item, dict) and item.get("type") in {"text", "reasoning", "error"}
                )
                if role == "assistant" and not text:
                    continue
                position += 1
                messages.append(MemorySourceMessage(position, f"{turn.id}:{turn.current_data_idx}:{role}", role, text))
        status = "running" if conversation.running else "completed"
        return MemorySessionSnapshot(conversation.thread_id, tuple(messages), status, conversation.project_id)


__all__ = ["MemoryConversation", "MemoryConversationSource"]
