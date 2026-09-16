from types import SimpleNamespace

from backend.providers.chat_completions.messages import _wire_messages_from
from backend.runtime.core.context.exchange import _chat_messages_from_nodes


def wire_history(blocks):
    node = SimpleNamespace(selected_messages=[{"role": "assistant", "content": blocks}])
    return _wire_messages_from(_chat_messages_from_nodes([node]))


def call(call_id, status="success"):
    return {
        "type": "tool_call",
        "call_id": call_id,
        "name": "container_exec",
        "arguments": {"command": call_id},
        "status": status,
    }


def tool_result(call_id, status="success"):
    return {
        "type": "tool_result",
        "call_id": call_id,
        "tool": "container_exec",
        "content": "result " + call_id,
        "status": status,
    }


def test_later_reasoning_text_and_tools_preserve_previous_wire_prefix():
    first = [
        {"type": "reasoning", "text": "Inspect first.", "status": "success"},
        {"type": "text", "text": "Reading files.", "status": "success"},
        call("a"),
        call("b"),
        tool_result("b"),
        tool_result("a"),
    ]
    previous = wire_history(first)
    second = first + [
        {"type": "reasoning", "text": "Now fix it.", "status": "success"},
        {"type": "text", "text": "Applying fix.", "status": "success"},
        call("c", "failed"),
        tool_result("c", "failed"),
    ]
    current = wire_history(second)
    assert current[: len(previous)] == previous
    assert [message["role"] for message in current] == ["assistant", "tool", "tool", "assistant", "tool"]
    assert (current[0]["reasoning_content"], current[3]["reasoning_content"]) == ("Inspect first.", "Now fix it.")
    assert (current[0]["content"], current[3]["content"]) == ("Reading files.", "Applying fix.")
    final = wire_history(second + [{"type": "text", "text": "Done.", "status": "success"}])
    assert final == current + [{"role": "assistant", "content": "Done."}]


def test_tool_only_round_and_pending_call_do_not_rewrite_prefix():
    first = [call("a"), tool_result("a")]
    previous = wire_history(first)
    current = wire_history(first + [call("b"), tool_result("b"), call("pending", "running")])
    assert current[: len(previous)] == previous
    assert [message["role"] for message in current] == ["assistant", "tool", "assistant", "tool"]
