from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from redis import Redis

from backend.domain import AssistantMessage, MessageQueueUnavailable, ToolMessage, UserMessage
from backend.planning.llm import LLMPlanner
from backend.runtime import AgentRunner, ConversationService
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.runtime.execution.todo_finalization import TODO_FINALIZATION_INSTRUCTION
from backend.storage.todo_list import MemoryTodoListStore, RedisTodoListStore
from backend.tools import build_tool_registry
from tests.local_store import session_store


class FinalizationPlanner:
    name = "todo-finalization"

    def __init__(self) -> None:
        self.saw_private_instruction = False

    def decide(self, runtime):
        if runtime.run.model_turns == 1:
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="update_todo_list",
                        call_id="todo-add",
                        arguments={
                            "expected_revision": 0,
                            "operations": [{"op": "add", "content": "unfinished", "status": "pending"}],
                        },
                    )
                ]
            )
        if runtime.run.model_turns == 2:
            assert runtime.exchange.on_content is not None
            runtime.exchange.on_content("discarded candidate")
            return AssistantMessage(content="discarded candidate")
        self.saw_private_instruction = any(
            isinstance(message, UserMessage) and message.content == TODO_FINALIZATION_INSTRUCTION
            for message in runtime.model_messages()
        )
        assert runtime.exchange.on_content is not None
        runtime.exchange.on_content("kept final")
        return AssistantMessage(content="kept final")


class CompletingPlanner:
    name = "todo-completing"

    def __init__(self, store) -> None:
        self.store = store

    def decide(self, runtime):
        if runtime.run.model_turns == 1:
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="update_todo_list",
                        call_id="todo-add",
                        arguments={
                            "expected_revision": 0,
                            "operations": [{"op": "add", "content": "finish", "status": "in_progress"}],
                        },
                    )
                ]
            )
        if runtime.run.model_turns == 2:
            todo_id = self.store.snapshot(runtime.state.session_id, runtime.run.turn_id).todos[0].id
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="update_todo_list",
                        call_id="todo-complete",
                        arguments={
                            "expected_revision": 1,
                            "operations": [{"op": "update", "id": todo_id, "status": "completed"}],
                        },
                    )
                ]
            )
        return AssistantMessage(content="done")


class CrashRecoveryPlanner:
    name = "todo-crash-recovery"

    def decide(self, runtime):
        if runtime.run.provenance.attempt > 1:
            return AssistantMessage(content="recovered")
        return AssistantMessage(
            tool_messages=[
                ToolMessage(
                    name="update_todo_list",
                    call_id="todo-crash",
                    arguments={
                        "expected_revision": 0,
                        "operations": [{"op": "add", "content": "committed", "status": "completed"}],
                    },
                )
            ]
        )


class FatalAfterTodoPlanner:
    name = "todo-fatal"

    def decide(self, runtime):
        if runtime.run.model_turns == 1:
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="update_todo_list",
                        call_id="todo-before-failure",
                        arguments={
                            "expected_revision": 0,
                            "operations": [{"op": "add", "content": "unfinished", "status": "pending"}],
                        },
                    )
                ]
            )
        raise RuntimeError("fatal after Todo")


class CrashAfterCommitStore:
    def __init__(self, delegate: RedisTodoListStore) -> None:
        self.delegate = delegate

    def update(self, **kwargs):
        self.delegate.update(**kwargs)
        raise KeyboardInterrupt

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)


class TrackingRedisTodoListStore(RedisTodoListStore):
    def __init__(self, client: Redis, *, key_prefix: str) -> None:
        super().__init__(client, key_prefix=key_prefix)
        self.persisted_ttls: list[int] = []

    def persist_turn(self, session_id: str, turn_id: str) -> None:
        super().persist_turn(session_id, turn_id)
        self.persisted_ttls.append(self.client.ttl(self._key(session_id, turn_id)))


class AutoCompactionTodoClient:
    context_size = 100

    def __init__(self, todo_store: RedisTodoListStore) -> None:
        self.todo_store = todo_store
        self.estimates = [10, 10, 100, 20, 10]
        self.decisions = 0
        self.source_turn_id = ""
        self.compacted_turn_id = ""
        self.compacted_request = []

    def estimate_tokens(self, _messages, _tools, _request_parameters) -> int:
        if len(self.estimates) > 1:
            return self.estimates.pop(0)
        return self.estimates[0]

    def run(self, runtime: AgentRuntime) -> PreparedResponse:
        if runtime.exchange.operation == "summarize":
            return PreparedResponse(AssistantMessage(content="compacted seed history"), {"total_tokens": 1})
        if runtime.exchange.operation != "decision":
            return PreparedResponse(AssistantMessage(content="local title"), {"total_tokens": 1})
        self.decisions += 1
        if self.decisions == 1:
            return PreparedResponse(AssistantMessage(content="seed complete"), {"total_tokens": 1})
        if self.decisions == 2:
            self.source_turn_id = runtime.run.turn_id
            return PreparedResponse(
                AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="update_todo_list",
                            call_id="auto-compact-add",
                            arguments={
                                "expected_revision": 0,
                                "operations": [
                                    {
                                        "op": "add",
                                        "content": "finish after automatic compaction",
                                        "status": "in_progress",
                                    }
                                ],
                            },
                        )
                    ]
                ),
                {"total_tokens": 1},
            )
        if self.decisions == 3:
            self.compacted_turn_id = runtime.run.turn_id
            self.compacted_request = list(runtime.exchange.messages)
            snapshot = self.todo_store.snapshot(runtime.state.session_id, runtime.run.turn_id)
            return PreparedResponse(
                AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="update_todo_list",
                            call_id="auto-compact-complete",
                            arguments={
                                "expected_revision": snapshot.revision,
                                "operations": [{"op": "update", "id": snapshot.todos[0].id, "status": "completed"}],
                            },
                        )
                    ]
                ),
                {"total_tokens": 1},
            )
        return PreparedResponse(AssistantMessage(content="completed after compaction"), {"total_tokens": 1})


@pytest.fixture
def redis_store() -> tuple[TrackingRedisTodoListStore, Redis]:
    client = Redis.from_url(os.environ.get("PRAXIS_TEST_REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    try:
        client.ping()
    except Exception as exc:
        client.close()
        pytest.skip(f"real Redis unavailable: {exc}")
    prefix = f"praxis:test:todo-runtime:{uuid4().hex}"
    store = TrackingRedisTodoListStore(client, key_prefix=prefix)
    yield store, client
    keys = list(client.scan_iter(f"{prefix}:*"))
    if keys:
        client.delete(*keys)
    client.close()


def test_finalization_discards_first_candidate_and_appends_authoritative_unfinished_list(tmp_path: Path) -> None:
    store = MemoryTodoListStore()
    planner = FinalizationPlanner()
    events = []
    runner = AgentRunner(planner, build_tool_registry(tmp_path), todo_store=store)
    runtime = runner.new_runtime(task="work", on_event=events.append)
    runtime.run.turn_id = "turn-finalization"
    result = runner.run(runtime)

    assert result.status == "completed"
    assert result.model_turns == 3
    assert planner.saw_private_instruction is True
    assert "discarded candidate" not in [event.message for event in events if event.kind == "response_delta"]
    assert "discarded candidate" not in [
        message.content for message in result.history if isinstance(message, AssistantMessage)
    ]
    assert result.final_answer is not None and result.final_answer.startswith("kept final")
    assert "todo_" in result.final_answer
    assert "[pending] unfinished" in result.final_answer


def test_completed_todos_end_without_a_finalization_pass(tmp_path: Path) -> None:
    store = MemoryTodoListStore()
    planner = CompletingPlanner(store)
    runner = AgentRunner(planner, build_tool_registry(tmp_path), todo_store=store)
    runtime = runner.new_runtime(task="work")
    runtime.run.turn_id = "turn-completed"
    result = runner.run(runtime)

    assert result.status == "completed"
    assert result.model_turns == 3
    assert result.final_answer == "done"
    assert store.finalization_claimed(runtime.state.session_id, result.turn_id) is False


def test_redis_receipt_repairs_sqlite_before_generic_resume_recovery(
    tmp_path: Path,
    redis_store: tuple[TrackingRedisTodoListStore, Redis],
) -> None:
    todo_store, _client = redis_store
    sqlite_store = session_store(tmp_path / "sqlite")
    runner = AgentRunner(
        CrashRecoveryPlanner(),
        build_tool_registry(tmp_path / "workspace"),
        checkpoints=sqlite_store,
        workspace_root=str(tmp_path.resolve()),
        todo_store=CrashAfterCommitStore(todo_store),
    )
    service = ConversationService(runner, sqlite_store)

    with pytest.raises(KeyboardInterrupt):
        service.run_task("commit once", mode="agent")

    assert service.active_session is not None
    session_id = service.active_session.session_id
    runtime = sqlite_store.load_runtime(session_id)
    assert runtime is not None and runtime.current_run is not None
    turn_id = runtime.current_run.turn_id
    assert todo_store.snapshot(session_id, turn_id).revision == 1
    crashed = sqlite_store.find_node(turn_id)
    assert crashed is not None
    crashed_items = [item for message in crashed.data[crashed.current_data_idx] for item in message["content"]]
    assert [item["type"] for item in crashed_items if item["type"].startswith("tool_")] == ["tool_call"]

    runner.todo_store = todo_store
    resumed = ConversationService(runner, sqlite_store).resume_session(session_id, resume_confirmed=True)

    assert resumed is not None and resumed.status == "completed"
    assert todo_store.snapshot(session_id, turn_id).revision == 1
    repaired = sqlite_store.find_node(turn_id)
    assert repaired is not None
    repaired_items = [item for message in repaired.data[repaired.current_data_idx] for item in message["content"]]
    tool_items = [item for item in repaired_items if item["type"].startswith("tool_")]
    assert [item["type"] for item in tool_items] == ["tool_call", "tool_result"]
    assert json.loads(tool_items[1]["content"])["revision"] == 1


def test_pause_resume_persists_then_completed_turn_expires(
    tmp_path: Path,
    redis_store: tuple[TrackingRedisTodoListStore, Redis],
) -> None:
    todo_store, client = redis_store
    planner = CrashRecoveryPlanner()
    sqlite_store = session_store(tmp_path / "sqlite")
    runner = AgentRunner(
        planner,
        build_tool_registry(tmp_path / "workspace"),
        checkpoints=sqlite_store,
        workspace_root=str(tmp_path.resolve()),
        todo_store=todo_store,
    )
    service = ConversationService(runner, sqlite_store)
    paused = service.run_task(
        "pause after Todo",
        mode="agent",
        suspend_requested=lambda: bool(
            service.runtime
            and service.runtime.state.current_run
            and todo_store.snapshot(
                service.runtime.state.session_id,
                service.runtime.state.current_run.turn_id,
            ).revision
        ),
    )

    assert paused.status == "cancelled"
    key = todo_store._key(service.runtime.state.session_id, paused.turn_id)  # type: ignore[union-attr]
    assert client.ttl(key) == -1
    client.expire(key, 60)

    resumed = ConversationService(runner, sqlite_store).resume_session(
        service.runtime.state.session_id,  # type: ignore[union-attr]
        resume_confirmed=True,
    )

    assert resumed is not None and resumed.status == "completed"
    assert -1 in todo_store.persisted_ttls
    assert 0 < client.ttl(key) <= 24 * 60 * 60


def test_real_runtime_automatic_compaction_copies_todo_and_continues_with_the_new_turn(
    tmp_path: Path,
    redis_store: tuple[TrackingRedisTodoListStore, Redis],
) -> None:
    todo_store, redis_client = redis_store
    registry = build_tool_registry(tmp_path / "workspace")
    client = AutoCompactionTodoClient(todo_store)
    planner = LLMPlanner(client, registry.specs(), registry.read_only_specs())
    sqlite_store = session_store(tmp_path / "sqlite")
    runner = AgentRunner(
        planner,
        registry,
        checkpoints=sqlite_store,
        workspace_root=str((tmp_path / "workspace").resolve()),
        todo_store=todo_store,
    )
    service = ConversationService(runner, sqlite_store)

    seed = service.run_task("seed enough completed history", mode="agent")
    assert seed.status == "completed"
    completed = service.run_task("continue with Todo through automatic compaction", mode="agent")

    assert completed.status == "completed"
    assert completed.final_answer == "completed after compaction"
    assert client.source_turn_id and client.compacted_turn_id
    assert client.compacted_turn_id != client.source_turn_id
    target = todo_store.snapshot(service.runtime.state.session_id, client.compacted_turn_id)  # type: ignore[union-attr]
    assert target.revision == 2
    assert [todo.status for todo in target.todos] == ["completed"]
    assert any(target.todos[0].id in str(message.content) for message in client.compacted_request)
    source_key = todo_store._key(service.runtime.state.session_id, client.source_turn_id)  # type: ignore[union-attr]
    target_key = todo_store._key(service.runtime.state.session_id, client.compacted_turn_id)  # type: ignore[union-attr]
    assert 0 < redis_client.ttl(source_key) <= 24 * 60 * 60
    assert 0 < redis_client.ttl(target_key) <= 24 * 60 * 60
    compacted_node = sqlite_store.find_node(client.compacted_turn_id)
    assert compacted_node is not None
    assert [item["type"] for item in compacted_node.assistant_items[:2]] == ["compaction", "todo_snapshot"]


def test_real_runtime_resumes_on_the_compacted_todo_turn(
    tmp_path: Path,
    redis_store: tuple[TrackingRedisTodoListStore, Redis],
) -> None:
    todo_store, redis_client = redis_store
    registry = build_tool_registry(tmp_path / "workspace")
    client = AutoCompactionTodoClient(todo_store)
    planner = LLMPlanner(client, registry.specs(), registry.read_only_specs())
    sqlite_store = session_store(tmp_path / "sqlite")
    runner = AgentRunner(
        planner,
        registry,
        checkpoints=sqlite_store,
        workspace_root=str((tmp_path / "workspace").resolve()),
        todo_store=todo_store,
    )
    service = ConversationService(runner, sqlite_store)
    assert service.run_task("seed enough completed history", mode="agent").status == "completed"

    paused = service.run_task(
        "pause after updating the automatically compacted Todo",
        mode="agent",
        suspend_requested=lambda: bool(
            client.compacted_turn_id
            and service.runtime
            and service.runtime.run.turn_id == client.compacted_turn_id
            and todo_store.snapshot(service.runtime.state.session_id, client.compacted_turn_id).revision == 2
        ),
    )

    assert paused.status == "cancelled"
    assert paused.turn_id == client.compacted_turn_id
    assert service.runtime is not None
    session_id = service.runtime.state.session_id
    source_key = todo_store._key(session_id, client.source_turn_id)
    target_key = todo_store._key(session_id, client.compacted_turn_id)
    assert 0 < redis_client.ttl(source_key) <= 24 * 60 * 60
    assert redis_client.ttl(target_key) == -1
    redis_client.expire(target_key, 60)

    resumed = ConversationService(runner, sqlite_store).resume_session(session_id, resume_confirmed=True)

    assert resumed is not None and resumed.status == "completed"
    assert resumed.turn_id == client.compacted_turn_id
    assert todo_store.snapshot(session_id, client.compacted_turn_id).revision == 2
    assert -1 in todo_store.persisted_ttls
    assert 0 < redis_client.ttl(target_key) <= 24 * 60 * 60


def test_failed_turn_expires_redis_state(
    tmp_path: Path,
    redis_store: tuple[TrackingRedisTodoListStore, Redis],
) -> None:
    todo_store, client = redis_store
    sqlite_store = session_store(tmp_path / "sqlite")
    runner = AgentRunner(
        FatalAfterTodoPlanner(),
        build_tool_registry(tmp_path / "workspace"),
        checkpoints=sqlite_store,
        workspace_root=str(tmp_path.resolve()),
        todo_store=todo_store,
    )
    service = ConversationService(runner, sqlite_store)

    with pytest.raises(RuntimeError, match="fatal after Todo"):
        service.run_task("fail after Todo", mode="agent")

    assert service.runtime is not None and service.runtime.state.current_run is not None
    key = todo_store._key(service.runtime.state.session_id, service.runtime.state.current_run.turn_id)
    assert 0 < client.ttl(key) <= 24 * 60 * 60


def test_unavailable_redis_fails_without_memory_fallback(tmp_path: Path) -> None:
    client = Redis(host="127.0.0.1", port=1, decode_responses=True, socket_connect_timeout=0.05, socket_timeout=0.05)
    store = RedisTodoListStore(client, key_prefix=f"praxis:test:unavailable:{uuid4().hex}")
    runner = AgentRunner(CrashRecoveryPlanner(), build_tool_registry(tmp_path), todo_store=store)
    runtime = runner.new_runtime(task="must use Redis")
    runtime.run.turn_id = "turn_unavailable"

    with pytest.raises(MessageQueueUnavailable, match="message_queue_unavailable"):
        runner.run(runtime)

    client.close()
