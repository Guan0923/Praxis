"""Local memory scheduling boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.configuration import ClientPaths
from backend.domain.memory import MemoryJobStatus
from backend.jobs import JobRegistry
from backend.runtime.memory import (
    MemoryAutomationService,
    MemoryAutomationSettings,
    MemoryConversation,
    MemorySessionSnapshot,
    MemorySourceMessage,
)
from backend.storage.memory import MemoryStore

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class _Settings:
    def __init__(self) -> None:
        self.enabled = True

    def memory_config(self):
        return {"enabled": self.enabled}


class _Source:
    def __init__(self, conversations: list[MemoryConversation]) -> None:
        self.conversations = {value.thread_id: value for value in conversations}
        self.messages = {
            value.thread_id: (
                MemorySourceMessage(
                    1, f"{value.thread_id}_v1_user", "user", "Remember this durable preference " + "x" * 80
                ),
                MemorySourceMessage(2, f"{value.thread_id}_v1_assistant", "assistant", "Understood."),
                MemorySourceMessage(
                    3, f"{value.thread_id}_v2_user", "user", "Keep using this preference in future reports " + "y" * 80
                ),
            )
            for value in conversations
        }

    def list(self):
        return list(self.conversations.values())

    def get(self, thread_id: str):
        return self.conversations.get(thread_id)

    def snapshot(self, conversation: MemoryConversation):
        return MemorySessionSnapshot(
            conversation.thread_id,
            self.messages[conversation.thread_id],
            "running" if conversation.running else "completed",
            conversation.project_id,
        )


class _State:
    def __init__(self, root: Path, source: _Source) -> None:
        self.settings = _Settings()
        self.memory_store = MemoryStore(ClientPaths(root))
        self.memory_source = source
        self.job_registry = JobRegistry()
        self.system_job_scope = self.job_registry.root_scope()


class _Model:
    def __init__(self, callback=lambda: None) -> None:
        self.callback = callback

    def extract_episodic(self, request):
        self.callback()
        user_id = [message.message_id for message in request.messages if message.role == "user"][-1]
        return {
            "candidates": [
                {
                    "title": "Stable preference",
                    "content": "The user prefers concise technical reports.",
                    "summary": "Concise reports",
                    "confidence": 0.9,
                    "tags": ["preference"],
                    "evidence_message_ids": [user_id],
                    "rediscoverable_from_source": False,
                }
            ]
        }

    def consolidate_memories(self, request):
        return {
            "added": [],
            "retained": [],
            "removed": [],
            "rejected_candidate_ids": [value.candidate_id for value in request.candidates],
        }


def _conversation(thread_id: str, *, age: timedelta, archived=False, deleted=False, running=False):
    return MemoryConversation(
        session_id=f"session_{thread_id}",
        thread_id=thread_id,
        project_id=None,
        updated_at=NOW - age,
        running=running,
        archived=archived,
        deleted=deleted,
    )


def _service(tmp_path: Path, source: _Source, model: _Model | None = None):
    state = _State(tmp_path / "data", source)
    service = MemoryAutomationService(
        state,
        lambda: model or _Model(),
        settings=MemoryAutomationSettings(phase1_concurrency=0),
        clock=lambda: NOW,
    )
    return state, service


def test_six_hour_boundary_applies_to_manual_extraction(tmp_path: Path) -> None:
    source = _Source(
        [
            _conversation("exact", age=timedelta(hours=6)),
            _conversation("recent", age=timedelta(hours=6) - timedelta(seconds=1)),
        ]
    )
    state, service = _service(tmp_path, source)
    try:
        assert service.enqueue_extract("exact").source_id == "exact"
        with pytest.raises(ValueError, match="6 hours"):
            service.enqueue_extract("recent")
    finally:
        state.job_registry.close_all(timeout=2)


def test_scan_times_each_thread_and_excludes_recycle_bin_and_running(tmp_path: Path) -> None:
    source = _Source(
        [
            _conversation("old", age=timedelta(hours=7)),
            _conversation("recent", age=timedelta(hours=1)),
            _conversation("archived", age=timedelta(hours=7), archived=True),
            _conversation("deleted", age=timedelta(hours=7), deleted=True),
            _conversation("running", age=timedelta(hours=7), running=True),
        ]
    )
    state, service = _service(tmp_path, source)
    try:
        service.scan_once()
        assert [job.source_id for job in state.memory_store.list_jobs()] == ["old"]
        with pytest.raises(ValueError, match="recycle bin"):
            service.enqueue_extract("archived")
        with pytest.raises(ValueError, match="recycle bin"):
            service.enqueue_extract("deleted")
    finally:
        state.job_registry.close_all(timeout=2)


@pytest.mark.parametrize("change", ["message", "archived", "deleted", "cancelled"])
def test_queued_change_prevents_write_and_watermark(tmp_path: Path, change: str) -> None:
    conversation = _conversation("thread_a", age=timedelta(hours=7))
    source = _Source([conversation])
    state, service = _service(tmp_path, source)
    queued = service.enqueue_extract("thread_a")
    claimed = state.memory_store.claim_job(service._worker_id, now="2030-01-01T00:00:00+00:00")
    assert claimed is not None

    def mutate() -> None:
        if change == "message":
            source.messages["thread_a"] += (
                MemorySourceMessage(4, "thread_a_v3_user", "user", "A new interaction arrived after queueing."),
            )
            source.conversations["thread_a"] = replace(conversation, updated_at=NOW)
        elif change in {"archived", "deleted"}:
            source.conversations["thread_a"] = replace(conversation, **{change: True})
        else:
            state.memory_store.cancel_job(queued.job_id, reason="test_cancel")

    service._model_factory = lambda: _Model(mutate)
    try:
        service._execute(claimed, is_cancelled=lambda: False)
        assert state.memory_store.get_watermark("thread_a") is None
        assert state.memory_store.list_items(include_deleted=True) == []
        assert state.memory_store.get_job(queued.job_id).status is MemoryJobStatus.CANCELLED
    finally:
        state.job_registry.close_all(timeout=2)
