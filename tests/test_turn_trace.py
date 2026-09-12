from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.session_store import session_store as web_session_store
from backend.api.state import WebAppState
from backend.domain import (
    AssistantMessage,
    RunState,
    SkillSnapshot,
    SystemMessage,
    TodoStateError,
    ToolSpec,
    TracePersistenceError,
    UserMessage,
)
from backend.domain.runtime_state import NodeWriter
from backend.domain.runtime_state import RuntimeState as TurnState
from backend.planning.model_requests import ModelRequestExecutor
from backend.runtime import AgentRunner, ConversationService
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.storage.todo_list import MemoryTodoListStore
from backend.tools import ToolRegistry
from tests.local_store import session_store


class RecordingClient:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, _runtime: AgentRuntime) -> PreparedResponse:
        self.calls += 1
        return PreparedResponse(AssistantMessage(content="ok"))


def bound_runtime(tmp_path: Path, *, user_content: str = "hello") -> tuple[AgentRuntime, object, TurnState]:
    store = session_store(tmp_path)
    session = store.create_session("Trace")
    root = store.ensure_root_node(session.session_id)
    turn = TurnState.create(
        session_id=session.session_id,
        thread_id=session.session_id,
        parent=root,
        user_content=user_content,
    )
    store.create_node(turn)
    runtime = AgentRuntime.ephemeral(session_id=session.session_id, planner=object(), tools=object())
    runtime.services.runtime_store = store
    runtime.state.provider = "responses"
    runtime.state.provider_name = "local-test"
    runtime.state.model = "fake-model"
    runtime.state.current_run = RunState(
        task=user_content,
        mode="agent",
        thread_id=turn.thread_id,
        turn_id=turn.id,
        data_idx=turn.current_data_idx,
        active_skills=[SkillSnapshot("audit", "Audit", "Secret: hidden", "C:/skill", "abc", source="project")],
    )
    return runtime, store, turn


def initialize_trace(
    runtime: AgentRuntime,
    *,
    system_message: str = "base\n\n## User Agent Preferences\nconcise",
    tools: list[ToolSpec] | None = None,
) -> None:
    runtime.exchange.context["trace_system_message"] = system_message
    ModelRequestExecutor(RecordingClient()).run(
        runtime,
        [SystemMessage(content="provider effective system"), UserMessage(content="provider transcript copy")],
        operation="decision",
        output_mode="tools",
        allowed_tools=tools or [],
        stream=False,
    )


def bound_bridge(runtime: AgentRuntime, store: object, turn: TurnState) -> RuntimeEventNodeBridge:
    bridge = RuntimeEventNodeBridge(
        store,
        session_id=turn.session_id,
        thread_id=turn.thread_id,
        source_node_id=turn.id,
        adopt_existing=True,
        message=None,
        emit=lambda _frame: None,
    )
    bridge.bind_runtime(runtime)
    bridge.start()
    return bridge


def test_automatic_compaction_moves_runtime_and_todo_to_the_compaction_turn(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path, user_content="continue")
    todo_store = MemoryTodoListStore()
    runtime.services.todo_store = todo_store
    added = todo_store.update(
        session_id=turn.session_id,
        turn_id=turn.id,
        call_id="todo-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "finish after compaction", "status": "in_progress"}],
    )
    assert todo_store.claim_finalization(turn.session_id, turn.id) is True
    bridge = bound_bridge(runtime, store, turn)

    bridge.handle(
        RuntimeEvent(
            "context_compaction_completed",
            "Conversation context compacted automatically",
            {
                "trigger": "automatic",
                "summary": "checkpoint",
                "target_turn_id": "turn-compacted",
                "todo_revision": added.snapshot.revision,
            },
        )
    )

    compacted = store.find_node("turn-compacted")
    assert isinstance(compacted, TurnState)
    assert runtime.run.turn_id == compacted.id
    assert runtime.run.data_idx == compacted.current_data_idx
    assert runtime.services.turn_trace_initialized is False
    assert todo_store.snapshot(turn.session_id, compacted.id) == added.snapshot
    assert todo_store.finalization_claimed(turn.session_id, compacted.id) is True
    assert [item["type"] for item in compacted.assistant_items[:2]] == ["compaction", "todo_snapshot"]
    todo_item = compacted.assistant_items[1]
    assert todo_item["source_turn_id"] == turn.id
    assert todo_item["target_turn_id"] == compacted.id
    assert todo_item["todos"] == [todo.to_dict() for todo in added.snapshot.todos]
    projected = runtime.model_messages()
    assert any(
        isinstance(message, UserMessage)
        and added.snapshot.todos[0].id in (message.content or "")
        and "finish after compaction" in (message.content or "")
        for message in projected
    )
    persisted_runtime = store.load_runtime(turn.session_id)
    assert persisted_runtime is not None and persisted_runtime.current_run is not None
    assert persisted_runtime.current_run.turn_id == compacted.id

    bridge.handle(
        RuntimeEvent(
            "context_compaction_completed",
            "Conversation context compacted automatically",
            {
                "trigger": "automatic",
                "summary": "second checkpoint",
                "target_turn_id": "turn-compacted-again",
                "todo_revision": added.snapshot.revision,
            },
        )
    )

    compacted_again = store.find_node("turn-compacted-again")
    assert isinstance(compacted_again, TurnState)
    assert [item["type"] for item in compacted_again.assistant_items].count("todo_snapshot") == 1
    assert compacted_again.assistant_items[1]["type"] == "todo_snapshot"
    assert todo_store.snapshot(turn.session_id, compacted_again.id) == added.snapshot


def test_automatic_compaction_expires_copied_todo_when_node_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingTodoStore(MemoryTodoListStore):
        def __init__(self) -> None:
            super().__init__()
            self.expired: list[tuple[str, str]] = []

        def expire_turn(self, session_id: str, turn_id: str) -> None:
            self.expired.append((session_id, turn_id))

    runtime, store, turn = bound_runtime(tmp_path, user_content="continue")
    todo_store = TrackingTodoStore()
    runtime.services.todo_store = todo_store
    added = todo_store.update(
        session_id=turn.session_id,
        turn_id=turn.id,
        call_id="todo-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "preserve source", "status": "pending"}],
    )
    bridge = bound_bridge(runtime, store, turn)

    def fail_create(*_args, **_kwargs):
        raise RuntimeError("node persistence failed")

    monkeypatch.setattr(store, "create_compact_turn", fail_create)

    with pytest.raises(RuntimeError, match="node persistence failed"):
        bridge.handle(
            RuntimeEvent(
                "context_compaction_completed",
                "Conversation context compacted automatically",
                {
                    "trigger": "automatic",
                    "summary": "checkpoint",
                    "target_turn_id": "turn-failed-target",
                    "todo_revision": added.snapshot.revision,
                },
            )
        )

    assert runtime.run.turn_id == turn.id
    assert store.find_node("turn-failed-target") is None
    assert todo_store.snapshot(turn.session_id, turn.id) == added.snapshot
    assert todo_store.expired == [(turn.session_id, "turn-failed-target")]


def test_automatic_compaction_copy_failure_keeps_the_source_turn_and_todo(tmp_path: Path) -> None:
    class FailingCopyTodoStore(MemoryTodoListStore):
        def copy_for_compaction(self, *_args, **_kwargs):
            raise TodoStateError("storage_unavailable", "copy failed")

    runtime, store, turn = bound_runtime(tmp_path, user_content="continue")
    todo_store = FailingCopyTodoStore()
    runtime.services.todo_store = todo_store
    added = todo_store.update(
        session_id=turn.session_id,
        turn_id=turn.id,
        call_id="todo-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "preserve source", "status": "pending"}],
    )
    bridge = bound_bridge(runtime, store, turn)

    with pytest.raises(TodoStateError, match="copy failed"):
        bridge.handle(
            RuntimeEvent(
                "context_compaction_completed",
                "Conversation context compacted automatically",
                {
                    "trigger": "automatic",
                    "summary": "checkpoint",
                    "target_turn_id": "turn-copy-failed",
                    "todo_revision": added.snapshot.revision,
                },
            )
        )

    assert runtime.run.turn_id == turn.id
    assert store.find_node("turn-copy-failed") is None
    assert todo_store.snapshot(turn.session_id, turn.id) == added.snapshot
    assert todo_store.snapshot(turn.session_id, "turn-copy-failed").revision == 0


def test_manual_compaction_does_not_copy_or_switch_the_active_todo_turn(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path, user_content="continue")
    todo_store = MemoryTodoListStore()
    runtime.services.todo_store = todo_store
    added = todo_store.update(
        session_id=turn.session_id,
        turn_id=turn.id,
        call_id="todo-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "stay on the source Turn", "status": "pending"}],
    )
    bridge = bound_bridge(runtime, store, turn)

    bridge.handle(
        RuntimeEvent(
            "context_compaction_completed",
            "Conversation context compacted manually",
            {"trigger": "manual", "summary": "manual checkpoint"},
        )
    )

    compacted = bridge.last_node
    assert isinstance(compacted, TurnState)
    assert compacted.id != turn.id
    assert all(item["type"] != "todo_snapshot" for item in compacted.assistant_items)
    assert runtime.run.turn_id == turn.id
    assert todo_store.snapshot(turn.session_id, turn.id) == added.snapshot
    assert todo_store.snapshot(turn.session_id, compacted.id).revision == 0


def test_automatic_compaction_without_initialized_todo_does_not_insert_snapshot(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path, user_content="continue")
    todo_store = MemoryTodoListStore()
    runtime.services.todo_store = todo_store
    bridge = bound_bridge(runtime, store, turn)

    bridge.handle(
        RuntimeEvent(
            "context_compaction_completed",
            "Conversation context compacted automatically",
            {
                "trigger": "automatic",
                "summary": "checkpoint without Todo",
                "target_turn_id": "turn-without-todo",
            },
        )
    )

    compacted = store.find_node("turn-without-todo")
    assert isinstance(compacted, TurnState)
    assert all(item["type"] != "todo_snapshot" for item in compacted.assistant_items)
    assert runtime.run.turn_id == compacted.id
    assert todo_store.snapshot(turn.session_id, compacted.id).revision == 0


def test_first_decision_initializes_one_redacted_context_and_current_user_item(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path, user_content="Token=private")
    client = RecordingClient()
    executor = ModelRequestExecutor(client)
    tools = [
        ToolSpec("read", "Read", {"type": "object"}),
        ToolSpec(
            "mcp_demo_search",
            "Search",
            {"type": "object", "password": "private"},
            {"praxis": {"trace_origin": {"kind": "mcp", "server": "demo", "tool": "search"}}},
        ),
    ]
    runtime.exchange.context["trace_system_message"] = "base\nCookie=private"

    executor.run(
        runtime,
        [SystemMessage(content="effective with injected skills"), UserMessage(content="duplicated transcript")],
        operation="decision",
        output_mode="tools",
        allowed_tools=tools,
        stream=False,
    )
    first = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert first is not None
    assert client.calls == 1
    assert first.context.system_message == "base\nCookie=[REDACTED]"
    assert first.context.active_skills[0]["instructions"] == "Secret:[REDACTED]"
    assert first.context.tools[0]["origin"] == {"kind": "local", "tool": "read"}
    assert first.context.tools[1]["origin"] == {"kind": "mcp", "server": "demo", "tool": "search"}
    assert first.context.tools[1]["parameters"]["password"] == "[REDACTED]"
    assert len(first.items) == 1
    assert first.items[0].role == "user"
    assert first.items[0].item["text"] == "Token=[REDACTED]"
    assert "duplicated transcript" not in str(first.to_dict())

    runtime.run.active_skills = []
    runtime.exchange.context["trace_system_message"] = "changed"
    executor.run(
        runtime,
        [SystemMessage(content="changed"), UserMessage(content="second transcript")],
        operation="decision",
        output_mode="tools",
        allowed_tools=[ToolSpec("write", "Write", {"type": "object"})],
    )
    second = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert second == first
    assert client.calls == 2
    assert runtime.run.model_calls == 2


def test_auxiliary_model_requests_do_not_create_or_change_trace(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    executor = ModelRequestExecutor(RecordingClient())
    executor.run(
        runtime,
        [SystemMessage(content="selector"), UserMessage(content="hello")],
        operation="skill_selection",
        output_mode="json",
    )
    assert store.load_turn_trace(turn.session_id, turn.id, 0) is None

    initialize_trace(runtime)
    expected = store.load_turn_trace(turn.session_id, turn.id, 0)
    executor.run(
        runtime,
        [SystemMessage(content="finalizer"), UserMessage(content="hello")],
        operation="finalize",
        output_mode="text",
    )
    assert store.load_turn_trace(turn.session_id, turn.id, 0) == expected


def test_preinitialization_skill_item_is_not_backfilled_and_stream_item_is_appended_once(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    bridge = bound_bridge(runtime, store, turn)
    bridge.handle(RuntimeEvent("skills_selected", "audit", {"skills": ["audit"], "source": "llm"}))
    initialize_trace(runtime)

    baseline = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert baseline is not None
    assert [item.role for item in baseline.items] == ["user", "developer"]

    bridge.handle(RuntimeEvent("thinking_start", "", {}))
    bridge.handle(RuntimeEvent("thinking_delta", "part one", {}))
    bridge.handle(RuntimeEvent("thinking_delta", " part two", {}))
    during_stream = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert during_stream is not None and during_stream.last_sequence == 2

    bridge.handle(RuntimeEvent("thinking_end", "", {}))
    completed = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert completed is not None
    assert completed.last_sequence == 3
    assert completed.items[-1].item == {
        "type": "reasoning",
        "text": "part one part two",
        "status": "success",
    }


def test_tool_call_result_and_steering_are_appended_in_canonical_order(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    bridge = bound_bridge(runtime, store, turn)
    initialize_trace(runtime)
    bridge.handle(
        RuntimeEvent(
            "assistant_message",
            "",
            {
                "message": {
                    "role": "assistant",
                    "tool_messages": [{"name": "read_file", "call_id": "call_1", "arguments": {"path": "README.md"}}],
                }
            },
        )
    )
    running = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert running is not None and running.last_sequence == 2

    bridge.handle(
        RuntimeEvent(
            "tool_result",
            "done",
            {"tool": "read_file", "call_id": "call_1", "result": {"content": "ok"}},
        )
    )
    bridge.handle(
        RuntimeEvent(
            "steering_applied",
            "In-run user input applied",
            {"content": "continue differently", "steering_id": "steer_1"},
        )
    )
    trace = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert trace is not None
    assert [item.item["type"] for item in trace.items] == ["text", "text", "tool_call", "tool_result", "text"]
    assert [item.role for item in trace.items] == ["user", "developer", "assistant", "assistant", "user"]
    assert trace.items[2].item["status"] == "success"

    duplicate = store.append_turn_trace_item(
        turn.session_id,
        turn.id,
        0,
        message_idx=trace.items[-1].message_idx,
        item_idx=trace.items[-1].item_idx,
        role="user",
        item=trace.items[-1].item,
        completed_at="later",
    )
    assert duplicate is not None and duplicate.last_sequence == trace.last_sequence


def test_model_retry_is_audited_after_the_next_attempt_starts(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    bridge = bound_bridge(runtime, store, turn)
    initialize_trace(runtime)

    bridge.handle(
        RuntimeEvent(
            "model_retry",
            "connection reset by peer",
            {"attempt": 1, "max_transport_retries": 2, "delay_seconds": 0.25},
        )
    )
    during_delay = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert during_delay is not None
    assert [item.role for item in during_delay.items] == ["user", "developer"]

    bridge.handle(RuntimeEvent("model_request", "Model decision request"))
    trace = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert trace is not None
    assert trace.items[-1].item == {
        "type": "retry",
        "event": "model_retry",
        "category": "network",
        "message": "connection reset by peer",
        "attempt": 1,
        "max_retries": 2,
        "delay_seconds": 0.25,
        "status": "success",
    }


def test_model_retry_finalization_redacts_and_audits_once(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    bridge = bound_bridge(runtime, store, turn)
    initialize_trace(runtime)

    bridge.handle(
        RuntimeEvent(
            "model_retry",
            "connection failed; Token:super-secret",
            {"attempt": 1, "max_transport_retries": 2, "delay_seconds": 0.25},
        )
    )
    completed = bridge.finish("success", "recovered")
    assert completed is not None and completed.status == "success"
    bridge.finish("success", "must not duplicate")

    trace = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert trace is not None
    retry_items = [item.item for item in trace.items if item.item.get("type") == "retry"]
    assert retry_items == [
        {
            "type": "retry",
            "event": "model_retry",
            "category": "network",
            "message": "connection failed; Token:[REDACTED]",
            "attempt": 1,
            "max_retries": 2,
            "delay_seconds": 0.25,
            "status": "success",
        }
    ]


def test_resumed_bridge_rebinds_existing_trace_before_projecting_items(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    initialize_trace(runtime)
    runtime.services.turn_trace_initialized = False

    bridge = bound_bridge(runtime, store, turn)
    assert runtime.services.turn_trace_initialized is True
    bridge.handle(RuntimeEvent("response_start", "", {}))
    bridge.handle(RuntimeEvent("response_delta", "resumed", {}))
    bridge.handle(RuntimeEvent("response_end", "", {}))

    trace = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert trace is not None
    assert [item.item["text"] for item in trace.items if item.item["type"] == "text" and item.role != "developer"] == [
        "hello",
        "resumed",
    ]


def test_child_turn_ignores_predecision_items_until_its_trace_is_initialized(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    bridge = bound_bridge(runtime, store, turn)
    initialize_trace(runtime)

    child = bridge.start_child("follow up", running_mode="agent")
    runtime.run.turn_id = child.id
    runtime.run.thread_id = child.thread_id
    runtime.run.data_idx = child.current_data_idx
    assert runtime.services.turn_trace_initialized is False

    bridge.handle(RuntimeEvent("skills_selected", "audit", {"skills": ["audit"], "source": "llm"}))
    assert store.load_turn_trace(child.session_id, child.id, child.current_data_idx) is None

    initialize_trace(runtime)
    trace = store.load_turn_trace(child.session_id, child.id, child.current_data_idx)
    assert trace is not None
    assert [item.role for item in trace.items] == ["user", "developer"]
    assert trace.items[0].item["text"] == "follow up"


def test_trace_initialization_failure_prevents_transport(tmp_path: Path) -> None:
    runtime, _store, _turn = bound_runtime(tmp_path)
    client = RecordingClient()
    runtime.services.runtime_store = object()

    with pytest.raises(TracePersistenceError, match="not sent"):
        ModelRequestExecutor(client).run(
            runtime,
            [SystemMessage(content="system"), UserMessage(content="hello")],
            operation="decision",
            output_mode="text",
        )

    assert client.calls == 0
    assert runtime.run.model_calls == 0


def test_trace_initialization_failure_marks_canonical_turn_failed(tmp_path: Path, monkeypatch) -> None:
    class Planner:
        name = "trace-failure"

        def __init__(self, client: RecordingClient) -> None:
            self.executor = ModelRequestExecutor(client)

        def decide(self, runtime: AgentRuntime) -> AssistantMessage:
            runtime.exchange.context["trace_system_message"] = "system"
            return self.executor.run(
                runtime,
                [SystemMessage(content="system"), *runtime.state.messages],
                operation="decision",
                output_mode="text",
            ).message

    store = session_store(tmp_path / "store")
    client = RecordingClient()
    service = ConversationService(AgentRunner(Planner(client), ToolRegistry(), checkpoints=store), store)

    def fail_trace(*_args, **_kwargs) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "initialize_turn_trace", fail_trace)
    with pytest.raises(TracePersistenceError, match="not sent"):
        service.run_task("audit me", mode="agent")

    assert client.calls == 0
    assert service.active_session is not None
    state = store.load_runtime(service.active_session.session_id)
    assert state is not None and state.current_run is not None
    turn = store.find_node(state.current_run.turn_id)
    assert turn is not None and turn.status == "failed"
    assert any(
        item.get("type") == "error" and item.get("message") == "disk unavailable"
        for message in turn.data[state.current_run.data_idx]
        for item in message["content"]
    )


def test_trace_item_failure_stops_turn_without_recursive_trace_writes(tmp_path: Path, monkeypatch) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    bridge = bound_bridge(runtime, store, turn)
    initialize_trace(runtime)

    def fail_item(*_args, **_kwargs) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "append_turn_trace_item", fail_item)
    bridge.handle(RuntimeEvent("response_start", "", {}))
    bridge.handle(RuntimeEvent("response_delta", "partial", {}))
    with pytest.raises(TracePersistenceError, match="Turn was stopped") as raised:
        bridge.handle(RuntimeEvent("response_end", "", {}))
    failed = bridge.finish_exception(raised.value)
    assert failed is not None and failed.status == "failed"
    assert bridge.trace_persistence_failed is True
    assert any(item.get("type") == "error" for message in failed.selected_messages for item in message["content"])


def test_trace_versions_are_isolated(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    turn.status = "success"
    store.finalize_node(turn)
    rewound = store.append_turn_version(
        turn.id,
        {"type": "text", "text": "version two", "status": "success"},
    )
    runtime.run.data_idx = rewound.current_data_idx
    initialize_trace(runtime, system_message="version two system")

    assert store.load_turn_trace(turn.session_id, turn.id, 0) is None
    trace = store.load_turn_trace(turn.session_id, turn.id, 1)
    assert trace is not None
    assert trace.context.system_message == "version two system"
    assert trace.items[0].item["text"] == "version two"


def test_turn_trace_api_returns_baseline_and_sequence_delta(tmp_path: Path, monkeypatch) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = web_session_store(state)
        root = store.ensure_root_node(sidebar["session_id"])
        turn = TurnState.create(
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            parent=root,
            user_content="hello",
        )
        store.create_node(turn)
        runtime = AgentRuntime.ephemeral(session_id=turn.session_id, planner=object(), tools=object())
        runtime.services.runtime_store = store
        runtime.state.current_run = RunState(
            task="hello",
            mode="agent",
            thread_id=turn.thread_id,
            turn_id=turn.id,
            data_idx=0,
        )
        initialize_trace(runtime)

        def unexpected_lookup(*_args, **_kwargs):
            raise AssertionError("Trace API must not load or search Turn records")

        def trace_request(params: dict[str, object]):
            with monkeypatch.context() as guard:
                guard.setattr(state, "access_session", unexpected_lookup)
                guard.setattr(state.session_store, "find_node", unexpected_lookup)
                guard.setattr(state.session_store, "list_sessions", unexpected_lookup)
                guard.setattr(state.session_store, "load_nodes", unexpected_lookup)
                guard.setattr(state.session_store, "_json_object", unexpected_lookup)
                return client.get(f"/api/turns/{turn.id}/trace", params=params)

        params = {
            "session_id": turn.session_id,
            "thread_id": turn.thread_id,
            "data_idx": 0,
        }
        baseline = trace_request(params)
        assert baseline.status_code == 200
        assert set(baseline.json()) == {"context", "items", "last_sequence"}
        assert baseline.json()["context"]["system_message"].startswith("base")
        assert [item["sequence"] for item in baseline.json()["items"]] == [1]
        assert baseline.json()["last_sequence"] == 1
        wrong_thread = trace_request({**params, "thread_id": "another-thread"})
        assert wrong_thread.status_code == 200
        assert wrong_thread.json() == {"context": None, "items": [], "last_sequence": 0}
        assert trace_request({"data_idx": 0}).status_code == 422

        writer = NodeWriter(store)
        current = store.get_node(turn.session_id, turn.id)
        assert isinstance(current, TurnState)
        updated = writer.append_items(
            current,
            [{"type": "text", "text": "answer", "status": "success"}],
            message_idx=1,
        )
        stored = store.append_turn_trace_item(
            turn.session_id,
            turn.id,
            0,
            message_idx=1,
            item_idx=0,
            role="assistant",
            item=updated.data[0][1]["content"][0],
            completed_at="2026-08-28T00:00:02Z",
        )
        assert stored is not None

        delta = trace_request({**params, "after_sequence": 1})
        assert delta.status_code == 200
        assert delta.json()["context"] is None
        assert [item["sequence"] for item in delta.json()["items"]] == [2]
        assert delta.json()["last_sequence"] == 2
        assert trace_request({**params, "after_sequence": -1}).status_code == 422


def test_trace_api_returns_empty_shape_before_initialization(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web-empty")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = web_session_store(state)
        root = store.ensure_root_node(sidebar["session_id"])
        turn = TurnState.create(
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            parent=root,
            user_content="hello",
        )
        store.create_node(turn)
        requests = (
            (turn.session_id, turn.thread_id, turn.id, 0),
            (turn.session_id, turn.thread_id, "missing", 0),
            (turn.session_id, turn.thread_id, turn.id, 99),
            (turn.session_id, "another-thread", turn.id, 0),
            ("session_missing", "thread_missing", "turn_missing", 0),
        )
        for session_id, thread_id, turn_id, data_idx in requests:
            response = client.get(
                f"/api/turns/{turn_id}/trace",
                params={"session_id": session_id, "thread_id": thread_id, "data_idx": data_idx},
            )
            assert response.status_code == 200
            assert response.json() == {"context": None, "items": [], "last_sequence": 0}
