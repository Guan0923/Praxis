"""Conversation-source rules for local memory extraction."""

from __future__ import annotations

from types import SimpleNamespace

from backend.domain.sidebar_thread import SidebarThread, SidebarThreadSummary
from backend.runtime.memory import MemoryConversationSource


class _Sessions:
    def __init__(self) -> None:
        self.runtimes = {
            "main": SimpleNamespace(origin_kind="main", running_turn_id=None),
            "fork": SimpleNamespace(origin_kind="fork", running_turn_id="turn_running"),
            "subagent": SimpleNamespace(origin_kind="subagent", running_turn_id=None),
        }

    def list_sidebar_thread_summaries(self, *, state: str):
        assert state == "all"
        return [
            _summary("main", last_activity="2026-09-09T00:00:00+00:00", updated="2026-09-09T11:00:00+00:00"),
            _summary("fork", last_activity="2026-09-08T23:00:00+00:00", archived="2026-09-09T01:00:00+00:00"),
            _summary("subagent", last_activity="2026-09-08T22:00:00+00:00", deleted="2026-09-09T01:00:00+00:00"),
        ]

    def get_runtime_thread(self, _session_id: str, thread_id: str):
        return self.runtimes[thread_id]


class _Projects:
    @staticmethod
    def session_project(_session_id: str):
        return None


def _summary(
    thread_id: str,
    *,
    last_activity: str,
    updated: str = "2026-09-09T02:00:00+00:00",
    archived: str | None = None,
    deleted: str | None = None,
) -> SidebarThreadSummary:
    return SidebarThreadSummary(
        SidebarThread(
            thread_id=thread_id,
            session_id="session_root",
            title=thread_id,
            created_at="2026-09-08T00:00:00+00:00",
            updated_at=updated,
            last_activity_at=last_activity,
            archived_at=archived,
            deleted_at=deleted,
        ),
        message_count=2,
        conversation_updated_at=updated,
    )


def test_source_keeps_main_and_user_forks_but_excludes_subagents() -> None:
    conversations = MemoryConversationSource(_Sessions(), _Projects()).list()

    assert [value.thread_id for value in conversations] == ["main", "fork"]
    assert conversations[0].updated_at.isoformat() == "2026-09-09T00:00:00+00:00"
    assert conversations[0].running is False
    assert conversations[1].running is True
    assert conversations[1].archived is True
    assert conversations[1].deleted is False
