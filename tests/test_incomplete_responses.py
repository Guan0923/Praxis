"""Output-limit recovery through real HTTP, tools, and canonical Turn storage."""

from __future__ import annotations

import copy
import json
import threading
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.domain.runtime_state import RuntimeState as TurnState
from backend.planning import LLMPlanner
from backend.providers import LLMClient, ModelConfig
from backend.providers.errors import ModelResponseError, ModelTransportError
from backend.runtime import AgentRunner
from backend.runtime.core.context import AgentRuntime
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.tools import Tool, ToolError, ToolRegistry
from tests.local_store import session_store

PROTOCOLS = ("chat_completions", "responses", "messages")
CONTINUE_MARKER = "[Continue interrupted generation]"


def wire_response(protocol, *, incomplete=False, text="partial", calls=(), reasoning=""):
    usage = {"input_tokens": 10, "output_tokens": 5}
    if protocol == "chat_completions":
        return {
            "id": "completion-test",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length" if incomplete else "tool_calls" if calls else "stop",
                    "message": {
                        "role": "assistant",
                        "content": text,
                        "reasoning_content": reasoning,
                        "tool_calls": [
                            {
                                "id": ident,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args) if isinstance(args, dict) else args,
                                },
                            }
                            for ident, name, args in calls
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    if protocol == "responses":
        return {
            "id": "response-test",
            "model": "test-model",
            "status": "incomplete" if incomplete else "completed",
            "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": text}]},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": reasoning}]},
                *[
                    {
                        "type": "function_call",
                        "id": f"item-{ident}",
                        "call_id": ident,
                        "name": name,
                        "arguments": json.dumps(args) if isinstance(args, dict) else args,
                        "status": "completed" if isinstance(args, dict) else "incomplete",
                    }
                    for ident, name, args in calls
                ],
            ],
            "usage": usage,
        }
    return {
        "id": "message-test",
        "model": "test-model",
        "type": "message",
        "role": "assistant",
        "stop_reason": "max_tokens" if incomplete else "tool_use" if calls else "end_turn",
        "content": [
            {"type": "text", "text": text},
            {"type": "thinking", "thinking": reasoning},
            *[{"type": "tool_use", "id": ident, "name": name, "input": args} for ident, name, args in calls],
        ],
        "usage": usage,
    }


def stream_events(protocol, response):
    if protocol == "chat_completions":
        choice = response["choices"][0]
        message = copy.deepcopy(choice["message"])
        for index, tool in enumerate(message.get("tool_calls", [])):
            tool["index"] = index
        return [{**response, "choices": [{"index": 0, "delta": message, "finish_reason": choice["finish_reason"]}]}]
    if protocol == "responses":
        events = []
        for item in response.get("output", []):
            if item["type"] == "message":
                events.append({"type": "response.output_text.delta", "delta": item["content"][0]["text"]})
            elif item["type"] == "reasoning":
                events.append({"type": "response.reasoning_summary_text.delta", "delta": item["summary"][0]["text"]})
            else:
                events.append({"type": "response.output_item.added", "item": item})
        return [*events, {"type": f"response.{response['status']}", "response": response}]
    events = [
        {
            "type": "message_start",
            "message": {
                **response,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        }
    ]
    for index, block in enumerate(response["content"]):
        index *= 2  # Include text/thinking before nonconsecutive tool indices.
        kind = block["type"]
        field = {"text": "text", "thinking": "thinking", "tool_use": "input"}[kind]
        start = {**block, field: {} if kind == "tool_use" else ""}
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        if kind == "tool_use":
            args = block["input"]
            delta = {"type": "input_json_delta", "partial_json": json.dumps(args) if isinstance(args, dict) else args}
        else:
            delta = {"type": f"{kind}_delta", field: block[field]}
        events.append({"type": "content_block_delta", "index": index, "delta": delta})
        if kind != "tool_use" or isinstance(block["input"], dict):
            events.append({"type": "content_block_stop", "index": index})
    return [
        *events,
        {"type": "message_delta", "delta": {"stop_reason": response["stop_reason"]}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]


def parse_response(protocol, response, *, stream):
    client = LLMClient(ModelConfig("test-key", "http://127.0.0.1:1/v1", "test-model", protocol=protocol))
    runtime = AgentRuntime.ephemeral(session_id="test", planner=object(), tools=object())
    runtime.exchange.raw_response = iter(stream_events(protocol, response)) if stream else response
    return client.llm.prepare_response(runtime)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("incomplete", [False, True])
def test_output_limit_keeps_complete_tools_and_usage(protocol, stream, incomplete):
    calls = [("one", "inspect", {"label": "one"}), ("two", "inspect", {"label": "two"})]
    if incomplete:
        calls.append(("unfinished", "inspect", '{"label":'))
    result = parse_response(protocol, wire_response(protocol, calls=calls, incomplete=incomplete), stream=stream)
    assert result.incomplete_reason == ("output_limit" if incomplete else None)
    assert [(tool.call_id, tool.arguments) for tool in result.message.tool_messages] == [
        ("one", {"label": "one"}),
        ("two", {"label": "two"}),
    ]
    assert result.message.content == "partial"
    assert result.usage.get("input_tokens", result.usage.get("prompt_tokens")) == 10
    assert result.usage.get("output_tokens", result.usage.get("completion_tokens")) == 5


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
def test_empty_truncation_is_not_fabricated_as_an_empty_tool_call(protocol, stream):
    response = wire_response(protocol, incomplete=True, text="", calls=[("unfinished", "inspect", "")])
    result = parse_response(protocol, response, stream=stream)
    assert result.incomplete_reason == "output_limit"
    assert result.message.tool_messages == []


@pytest.mark.parametrize("reason", ["content_filter", "unknown", None])
@pytest.mark.parametrize("stream", [False, True])
def test_responses_nonrecoverable_incomplete_does_not_enter_output_repair(reason, stream):
    response = wire_response("responses", incomplete=True)
    response["incomplete_details"] = {"reason": reason}
    with pytest.raises(ModelResponseError) as error:
        parse_response("responses", response, stream=stream)
    assert error.value.diagnostics["incomplete_details"]["reason"] == reason


@pytest.mark.parametrize("protocol", ["responses", "messages"])
def test_missing_stream_terminal_is_not_a_success(protocol):
    response = wire_response(protocol)
    runtime = AgentRuntime.ephemeral(session_id="test", planner=object(), tools=object())
    runtime.exchange.raw_response = iter(stream_events(protocol, response)[:-1])
    client = LLMClient(ModelConfig("test-key", "http://127.0.0.1:1/v1", "test-model", protocol=protocol))
    with pytest.raises(ModelResponseError):
        client.llm.prepare_response(runtime)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_transport_failure_is_not_converted_to_output_repair(protocol):
    def broken():
        yield from ()
        raise ModelTransportError("disconnected", retryable=False, stream_started=True)

    runtime = AgentRuntime.ephemeral(session_id="test", planner=object(), tools=object())
    runtime.exchange.raw_response = broken()
    client = LLMClient(ModelConfig("test-key", "http://127.0.0.1:1/v1", "test-model", protocol=protocol))
    with pytest.raises(ModelTransportError):
        client.llm.prepare_response(runtime)


@contextmanager
def local_provider(protocol, responses, before_response=None):
    pending = deque(responses)
    received = []
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(payload)
            try:
                if before_response is not None:
                    before_response(len(received), payload)
                response = pending.popleft()
                if payload.get("stream"):
                    body = "".join(f"data: {json.dumps(event)}\n\n" for event in stream_events(protocol, response))
                    if protocol == "chat_completions":
                        body += "data: [DONE]\n\n"
                    mime = "text/event-stream"
                else:
                    body, mime = json.dumps(response), "application/json"
            except Exception as exc:
                errors.append(exc)
                self.send_error(400, "Unexpected test request")
                return
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
        if errors:
            raise errors[0]


@contextmanager
def canonical_runner(tmp_path, protocol, base_url, tools, *, mode="agent", on_event=None, interrupt=None):
    if not tools.names():
        tools = ToolRegistry([Tool("unused", "Unused test tool.", lambda: "unused")])
    store = session_store(tmp_path / "data")
    session = store.create_session("Incomplete response test")
    client = LLMClient(ModelConfig("test-key", base_url, "test-model", protocol=protocol))
    planner = LLMPlanner(client, tools.specs(), read_only_tool_specs=tools.read_only_specs())
    runner = AgentRunner(planner, tools, max_transport_retries=0, skills_enabled=False)
    runtime = runner.new_runtime(
        task="finish the task",
        session_id=session.session_id,
        mode=mode,
        runtime_store=store,
        interrupt=interrupt,
    )
    frames, events = [], []
    bridge = RuntimeEventNodeBridge(
        store,
        session_id=session.session_id,
        prompt="finish the task",
        turn_id="turn-incomplete",
        provider=protocol,
        provider_name="local-test",
        model="test-model",
        running_mode=mode,
        emit=frames.append,
    )
    bridge.bind_runtime(runtime)
    turn = bridge.start()
    runtime.run.turn_id = turn.id
    runtime.run.thread_id = turn.thread_id
    runtime.run.data_idx = turn.current_data_idx

    def publish(event):
        events.append(event)
        bridge.handle(event)
        if on_event is not None:
            on_event(event)

    runtime.services.on_event = publish
    try:
        yield runner, runtime, bridge, store, frames, events
    finally:
        runner.close()
        client.transport.session.close()


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_real_http_waits_for_tools_and_continues_one_sqlite_turn(tmp_path, protocol, mode):
    for label in ("fast", "slow"):
        (tmp_path / f"{label}.txt").write_text(f"result-{label}", encoding="utf-8")
    entered = threading.Barrier(2)
    fast_published = threading.Event()
    executed = []
    result_ids = []

    def inspect(label: str) -> str:
        entered.wait(5)
        if label == "slow" and not fast_published.wait(5):
            raise ToolError("The fast tool result was not published.")
        result = (tmp_path / f"{label}.txt").read_text(encoding="utf-8")
        executed.append(label)
        return result

    def event_received(event):
        if event.kind == "tool_result":
            result_ids.append(event.data["call_id"])
            if event.data["call_id"] == "fast":
                fast_published.set()

    def before_response(number, payload):
        if number == 2:
            assert result_ids == ["fast", "slow"]
            encoded = json.dumps(payload)
            assert "result-fast" in encoded and "result-slow" in encoded
            assert '"unfinished"' not in encoded
            assert CONTINUE_MARKER in encoded
            assert "partial" in encoded
            assert bridge.writer.current(runtime.state.session_id, runtime.run.turn_id).status == "running"

    tools = ToolRegistry(
        [
            Tool(
                "inspect",
                "Read a local result.",
                inspect,
                parameters={
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                    "required": ["label"],
                },
            )
        ]
    )
    replies = [
        wire_response(
            protocol,
            incomplete=True,
            calls=[
                ("slow", "inspect", {"label": "slow"}),
                ("fast", "inspect", {"label": "fast"}),
                ("unfinished", "inspect", '{"label":'),
            ],
        ),
        wire_response(protocol, text="done"),
    ]
    with local_provider(protocol, replies, before_response) as (url, received):
        with canonical_runner(tmp_path, protocol, url, tools, mode=mode, on_event=event_received) as bound:
            runner, runtime, bridge, store, frames, events = bound
            initial = bridge.start()
            result = runner.run(runtime)
            assert result.status == "completed", result.final_answer
            terminal = bridge.finish("success")
            assert len(received) == 2 and executed == ["fast", "slow"]
            assert terminal.id == initial.id and terminal.version == initial.version
            assert terminal.current_data_idx == initial.current_data_idx
            assert (
                len([node for node in store.load_nodes(runtime.state.session_id) if isinstance(node, TurnState)]) == 1
            )
            texts = [item["text"] for item in terminal.assistant_items if item["type"] == "text"]
            assert texts == ["partial", "done"]
            assert len([message for message in terminal.selected_messages if message["role"] == "user"]) == 1
            assert runtime.state.token_usage["totals"] == {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
            assert len(runtime.state.token_usage["requests"]) == 2
            assert len([event for event in events if event.kind == "run_finished"]) == 1
            assert {frame.turn_id for frame in frames} == {initial.id}
            reopened = session_store(tmp_path / "data")
            assert reopened.get_node(runtime.state.session_id, initial.id).to_dict() == terminal.to_dict()
            trace = reopened.load_turn_trace(runtime.state.session_id, initial.id, 0)
            assert trace is not None
            assert len([item for item in trace.items if item.item.get("type") == "tool_result"]) == 2


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_text_and_reasoning_only_truncations_continue_without_a_new_turn(tmp_path, protocol, mode):
    replies = [
        wire_response(protocol, incomplete=True, text=""),
        wire_response(protocol, incomplete=True, text="", reasoning="thinking"),
        wire_response(protocol, incomplete=True, text="first part"),
        wire_response(protocol, text="last part"),
    ]
    with local_provider(protocol, replies) as (url, received):
        with canonical_runner(tmp_path, protocol, url, ToolRegistry(), mode=mode) as bound:
            runner, runtime, bridge, _store, _frames, events = bound
            result = runner.run(runtime)
            assert result.status == "completed", result.final_answer
            terminal = bridge.finish("success")
            assert len(received) == 4
            assert all(json.dumps(request).count(CONTINUE_MARKER) == 1 for request in received[1:])
            assert "first part" in json.dumps(received[3])
            assert [item["text"] for item in terminal.assistant_items if item["type"] == "text"] == [
                "first part",
                "last part",
            ]
            assert any(item.get("text") == "thinking" for item in terminal.assistant_items)
            assert runtime.run.retries == 0
            assert runtime.state.token_usage["totals"]["total_tokens"] == 60
            assert not runtime.exchange.continuation_pending
            assert not any(event.kind == "model_repair" for event in events)


@pytest.mark.parametrize("outcome", ["failure", "deny", "invalid_arguments"])
def test_failed_or_denied_tool_is_returned_before_continuing(tmp_path, outcome):
    invocations = []

    def inspect() -> str:
        invocations.append("called")
        raise ToolError("local tool failure")

    tool = Tool(
        "inspect",
        "Inspect.",
        inspect,
        requires_confirmation=outcome == "deny",
        parameters=(
            {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}
            if outcome == "invalid_arguments"
            else {}
        ),
    )
    replies = [
        wire_response("responses", incomplete=True, calls=[("one", "inspect", {})]),
        wire_response("responses", text="done"),
    ]
    with local_provider("responses", replies) as (url, received):
        with canonical_runner(
            tmp_path, "responses", url, ToolRegistry([tool]), interrupt=lambda _request: InterruptDecision("deny")
        ) as bound:
            runner, runtime, bridge, _store, _frames, _events = bound
            result = runner.run(runtime)
            assert result.status == "completed", result.final_answer
            terminal = bridge.finish("success")
            assert len(received) == 2
            expected = {"deny": "denied", "failure": "local tool failure", "invalid_arguments": "required"}[outcome]
            assert expected in json.dumps(received[1])
            assert invocations == (["called"] if outcome == "failure" else [])
            assert [item["status"] for item in terminal.assistant_items if item["type"] == "tool_result"] == ["failed"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_cancel_after_truncation_does_not_send_a_continuation(tmp_path, protocol):
    cancelled = threading.Event()

    def on_event(event):
        if event.kind == "assistant_message":
            cancelled.set()

    with local_provider(protocol, [wire_response(protocol, incomplete=True)]) as (url, received):
        with canonical_runner(tmp_path, protocol, url, ToolRegistry(), on_event=on_event) as bound:
            runner, runtime, bridge, _store, _frames, _events = bound
            runtime.services.cancel_requested = cancelled.is_set
            result = runner.run(runtime)
            assert result.status == "cancelled"
            assert len(received) == 1
            assert bridge.last_node.status == "paused"


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
def test_content_filter_is_terminal_even_with_unfinished_tools(protocol, stream):
    response = wire_response(protocol, incomplete=True, calls=[("unfinished", "inspect", '{"label":')])
    if protocol == "chat_completions":
        response["choices"][0]["finish_reason"] = "content_filter"
    elif protocol == "responses":
        response["incomplete_details"] = {"reason": "content_filter"}
    else:
        response["stop_reason"] = "refusal"
    with pytest.raises(ModelResponseError):
        parse_response(protocol, response, stream=stream)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_explicit_provider_error_preserves_message(protocol):
    response = {"type": "error", "error": {"message": "provider capacity exhausted"}}
    with pytest.raises(ModelResponseError, match="provider capacity exhausted"):
        parse_response(protocol, response, stream=False)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_streamed_provider_error_preserves_message(protocol):
    event = {"type": "error", "error": {"message": "provider capacity exhausted"}}
    if protocol == "responses":
        event = {"type": "error", "message": "provider capacity exhausted"}
    client = LLMClient(ModelConfig("test-key", "http://127.0.0.1:1/v1", "test-model", protocol=protocol))
    runtime = AgentRuntime.ephemeral(session_id="test", planner=object(), tools=object())
    runtime.exchange.raw_response = iter([event])
    with pytest.raises(ModelResponseError, match="provider capacity exhausted"):
        client.llm.prepare_response(runtime)


@pytest.mark.parametrize("protocol", ["responses", "messages"])
def test_unfinished_stream_tool_is_not_executed_even_with_valid_json(protocol):
    response = wire_response(protocol, incomplete=True, calls=[("unfinished", "inspect", {})])
    if protocol == "responses":
        response["output"][-1]["status"] = "in_progress"
    events = stream_events(protocol, response)
    if protocol == "messages":
        events = [event for event in events if not (event["type"] == "content_block_stop" and event["index"] == 4)]
    client = LLMClient(ModelConfig("test-key", "http://127.0.0.1:1/v1", "test-model", protocol=protocol))
    runtime = AgentRuntime.ephemeral(session_id="test", planner=object(), tools=object())
    runtime.exchange.raw_response = iter(events)
    result = client.llm.prepare_response(runtime)
    assert result.incomplete_reason == "output_limit"
    assert result.message.tool_messages == []


def test_nonrecoverable_incomplete_stops_without_another_http_request(tmp_path):
    response = wire_response("responses", incomplete=True)
    response["incomplete_details"] = {"reason": "content_filter"}
    with local_provider("responses", [response]) as (url, received):
        with canonical_runner(tmp_path, "responses", url, ToolRegistry()) as bound:
            runner, runtime, bridge, _store, _frames, _events = bound
            result = runner.run(runtime)
            assert result.status == "failed"
            assert "content_filter" in result.final_answer
            assert len(received) == 1
            assert bridge.last_node.status == "failed"


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_user_steering_during_tools_supersedes_continuation(tmp_path, mode):
    pending = deque()

    def inspect() -> str:
        pending.append("Follow the new instruction instead.")
        return "previous tool result"

    def drain():
        return [pending.popleft()] if pending else []

    replies = [
        wire_response("responses", incomplete=True, calls=[("one", "inspect", {})]),
        wire_response("responses", text="new answer"),
    ]
    with local_provider("responses", replies) as (url, received):
        with canonical_runner(
            tmp_path, "responses", url, ToolRegistry([Tool("inspect", "Inspect.", inspect)]), mode=mode
        ) as bound:
            runner, runtime, bridge, _store, _frames, _events = bound
            runtime.services.steering = drain
            result = runner.run(runtime)
            assert result.status == "completed", result.final_answer
            terminal = bridge.finish("success")
            assert len(received) == 2
            encoded = json.dumps(received[1])
            assert "Follow the new instruction instead." in encoded
            assert CONTINUE_MARKER not in encoded
            assert terminal.id == "turn-incomplete"


def test_cancel_waits_for_active_tool_batch_without_requesting_again(tmp_path):
    started = threading.Barrier(2)
    cancel = threading.Event()
    finished = []

    def inspect(label: str) -> str:
        started.wait(5)
        if label == "slow" and not cancel.wait(5):
            raise ToolError("Cancellation was not delivered.")
        finished.append(label)
        return label

    def event_received(event):
        if event.kind == "tool_result" and event.data["call_id"] == "fast":
            cancel.set()

    tools = ToolRegistry([Tool("inspect", "Inspect.", inspect)])
    response = wire_response(
        "responses",
        incomplete=True,
        calls=[
            ("slow", "inspect", {"label": "slow"}),
            ("fast", "inspect", {"label": "fast"}),
        ],
    )
    with local_provider("responses", [response]) as (url, received):
        with canonical_runner(tmp_path, "responses", url, tools, on_event=event_received) as bound:
            runner, runtime, _bridge, _store, _frames, _events = bound
            runtime.services.cancel_requested = cancel.is_set
            result = runner.run(runtime)
            assert result.status == "cancelled"
            assert finished == ["fast", "slow"]
            assert len(received) == 1
