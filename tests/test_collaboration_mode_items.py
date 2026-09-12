from pathlib import Path

import pytest

from backend.domain import AssistantMessage, DeveloperMessage, UserMessage
from backend.domain.execution_config import RuntimeConfigUpdate
from backend.domain.input_message import InputMessage
from backend.planning.llm import LLMPlanner
from backend.planning.prompts import collaboration_mode_prompt, compose_system_prompt
from backend.providers import ModelConfig
from backend.providers.canonical import to_chat_completions, to_messages, to_responses
from backend.providers.protocols import ChatCompletionsAdapter, MessagesAdapter, ResponsesAdapter
from backend.runtime import AgentRunner, PreparedResponse
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.tools import ToolRegistry
from tests.local_store import session_store


class RecordingClient:
    def __init__(self):
        self.requests = []

    def run(self, runtime):
        self.requests.append(list(runtime.exchange.messages))
        return PreparedResponse(AssistantMessage(content="hello"))


def conversation(tmp_path: Path, mode: str = "agent"):
    store = session_store(tmp_path)
    session = store.create_session("Modes")
    client = RecordingClient()
    planner = LLMPlanner(client, [], [])
    runner = AgentRunner(planner, ToolRegistry())
    runtime = runner.new_runtime(task="hello", mode=mode, session_id=session.session_id, runtime_store=store)
    bridge = RuntimeEventNodeBridge(
        store,
        session_id=session.session_id,
        message=InputMessage("hello"),
        running_mode=mode,
        emit=lambda frame: None,
    )
    bridge.bind_runtime(runtime)
    turn = bridge.start()
    runtime.run.turn_id, runtime.run.thread_id = turn.id, turn.thread_id
    runtime.run.data_idx = turn.current_data_idx
    return store, client, planner, runtime, bridge


def modes(bridge):
    return [
        message["content"][0]["text"]
        for message in bridge.assistant.selected_messages
        if message["role"] == "developer"
    ]


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_initial_mode_is_durable_traced_and_sent_as_user(tmp_path: Path, mode: str):
    store, client, planner, runtime, bridge = conversation(tmp_path, mode)
    planner.decide(runtime)
    planner.decide(runtime)
    expected = collaboration_mode_prompt(mode)
    assert [(message.role, message.content) for message in client.requests[0]] == [
        ("system", compose_system_prompt()),
        ("user", "hello"),
        ("user", expected),
    ]
    turn = store.get_node(runtime.state.session_id, runtime.run.turn_id)
    trace = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert [entry.item["text"] for entry in trace.items if entry.role == "developer"] == [expected]
    resumed = RuntimeEventNodeBridge(
        store,
        session_id=turn.session_id,
        source_node_id=turn.id,
        adopt_existing=True,
        message=None,
        running_mode=mode,
        emit=lambda frame: None,
    )
    resumed.bind_runtime(runtime)
    resumed.start()
    assert modes(resumed) == modes(bridge) == [expected]


@pytest.mark.parametrize(("initial", "target"), [("agent", "plan"), ("plan", "agent")])
@pytest.mark.parametrize("kind", ["response", "thinking"])
def test_mode_waits_until_stream_item_finishes(tmp_path: Path, initial: str, target: str, kind: str):
    store, client, planner, runtime, bridge = conversation(tmp_path, initial)
    planner.decide(runtime)
    bridge.handle(RuntimeEvent(f"{kind}_start"))
    bridge.handle(RuntimeEvent(f"{kind}_delta", "first"))
    bridge.apply_runtime_config(RuntimeConfigUpdate(running_mode=target))
    assert modes(bridge) == [collaboration_mode_prompt(initial)]
    bridge.handle(RuntimeEvent(f"{kind}_delta", " second"))
    bridge.handle(RuntimeEvent(f"{kind}_end"))
    assert modes(bridge) == [collaboration_mode_prompt(initial), collaboration_mode_prompt(target)]
    trace = store.load_turn_trace(runtime.state.session_id, runtime.run.turn_id, 0)
    assert [(entry.role, entry.item.get("text")) for entry in trace.items[-2:]] == [
        ("assistant", "first second"),
        ("developer", collaboration_mode_prompt(target)),
    ]
    runtime.apply_pending_runtime_config()
    planner.decide(runtime)
    assert runtime.run.mode == target
    assert client.requests[-1][-1] == UserMessage(content=collaboration_mode_prompt(target))
    assert "hello" in [message.content for message in runtime.model_messages(current_turn_only=True)]


def test_tool_results_remain_paired_across_mode_boundary(tmp_path: Path):
    store, client, planner, runtime, bridge = conversation(tmp_path)
    planner.decide(runtime)
    bridge.handle(
        RuntimeEvent(
            "assistant_message",
            data={
                "message": {
                    "tool_messages": [
                        {"name": "read_file", "call_id": "first", "arguments": {"path": "first"}},
                        {"name": "read_file", "call_id": "second", "arguments": {"path": "second"}},
                    ]
                }
            },
        )
    )
    bridge.apply_runtime_config(RuntimeConfigUpdate(running_mode="plan"))
    bridge.handle(RuntimeEvent("tool_result", data={"tool": "read_file", "call_id": "first", "result": "one"}))
    assert len(modes(bridge)) == 2
    bridge.handle(RuntimeEvent("tool_call", data={"tool": "read_file", "call_id": "second"}))
    bridge.handle(RuntimeEvent("tool_result", data={"tool": "read_file", "call_id": "second", "result": "two"}))
    projected = runtime.model_messages()
    calls = [tool for message in projected if isinstance(message, AssistantMessage) for tool in message.tool_messages]
    assert [(tool.call_id, tool.content) for tool in calls] == [("first", "one"), ("second", "two")]
    persisted = store.get_node(runtime.state.session_id, runtime.run.turn_id)
    assert (
        sum(item.get("type") == "tool_call" for message in persisted.selected_messages for item in message["content"])
        == 2
    )


def test_idle_switches_are_ordered_and_child_turn_gets_its_own_mode(tmp_path: Path):
    store, client, planner, runtime, bridge = conversation(tmp_path)
    bridge.apply_runtime_config(RuntimeConfigUpdate(running_mode="plan"))
    bridge.apply_runtime_config(RuntimeConfigUpdate(running_mode="plan"))
    bridge.apply_runtime_config(RuntimeConfigUpdate(running_mode="agent"))
    assert modes(bridge) == [collaboration_mode_prompt(mode) for mode in ("agent", "plan", "agent")]
    bridge.start_child("next", running_mode="plan")
    assert modes(bridge) == [collaboration_mode_prompt("plan")]


def test_pending_switches_finish_without_duplicating_the_answer(tmp_path: Path):
    store, client, planner, runtime, bridge = conversation(tmp_path)
    bridge.handle(RuntimeEvent("response_delta", "answer"))
    bridge.apply_runtime_config(RuntimeConfigUpdate(running_mode="plan"))
    bridge.apply_runtime_config(RuntimeConfigUpdate(running_mode="agent"))
    bridge.handle(RuntimeEvent("response_end"))
    finished = bridge.finish("success", "answer")
    assert modes(bridge) == [collaboration_mode_prompt(mode) for mode in ("agent", "plan", "agent")]
    assert [
        item["text"]
        for message in finished.selected_messages
        if message["role"] == "assistant"
        for item in message["content"]
        if item["type"] == "text"
    ] == ["answer"]


def test_switch_before_first_token_waits_for_the_inflight_model_item(tmp_path: Path):
    store, client, planner, runtime, bridge = conversation(tmp_path)
    bridge.handle(RuntimeEvent("model_request"))
    bridge.apply_runtime_config(RuntimeConfigUpdate(running_mode="plan"))
    assert modes(bridge) == [collaboration_mode_prompt("agent")]
    bridge.handle(RuntimeEvent("response_delta", "hello"))
    bridge.handle(RuntimeEvent("response_end"))
    assert modes(bridge) == [collaboration_mode_prompt("agent"), collaboration_mode_prompt("plan")]


@pytest.mark.parametrize(
    ("adapter_type", "converter"),
    [
        (ChatCompletionsAdapter, to_chat_completions),
        (ResponsesAdapter, to_responses),
        (MessagesAdapter, to_messages),
    ],
)
def test_all_provider_wire_formats_convert_developer_to_user(adapter_type, converter):
    text = collaboration_mode_prompt("plan")
    runtime = AgentRunner(object(), ToolRegistry()).new_runtime(task="hello")
    runtime.exchange.messages = [DeveloperMessage(content=text)]
    adapter = adapter_type(ModelConfig("unused", "https://unused.invalid/v1", "test"))
    payload = adapter.prepare_request(runtime)
    wire = payload.get("input", payload.get("messages"))
    canonical = converter([{"role": "developer", "content": [{"type": "text", "text": text, "status": "success"}]}])
    for message in [wire[0], canonical[0]]:
        assert message["role"] == "user"
        content = message["content"]
        assert (content if isinstance(content, str) else content[0]["text"]) == text
    assert runtime.exchange.messages[0].role == "developer"
