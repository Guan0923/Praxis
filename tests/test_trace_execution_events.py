from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.domain.execution_config import RuntimeConfigUpdate
from backend.runtime.conversation.trace import conversation_trace_records
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.persistence.recording import turn_trace_audit_value
from tests.test_turn_trace import bound_bridge, bound_runtime, initialize_trace


def test_sqlite_retry_reuses_positions_without_losing_audit_history(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    bridge = bound_bridge(runtime, store, turn)
    initialize_trace(runtime)
    for event in (
        RuntimeEvent("model_request", data={"exchange_id": "request-1", "transport": {"attempt": 1}}),
        RuntimeEvent("thinking_start"),
        RuntimeEvent("thinking_delta", "finished reasoning from failed attempt"),
        RuntimeEvent("thinking_end"),
        RuntimeEvent("response_start"),
        RuntimeEvent("response_delta", "unfinished answer"),
        RuntimeEvent("model_retry", "read timed out", {"attempt": 1, "max_transport_retries": 5}),
        RuntimeEvent("model_request", data={"exchange_id": "request-1", "transport": {"attempt": 2}}),
        RuntimeEvent("response_start"),
        RuntimeEvent("response_delta", "successful answer"),
        RuntimeEvent("response_end"),
        RuntimeEvent(
            "model_response", data={"exchange_id": "request-1", "usage": {"input_tokens": 12, "output_tokens": 3}}
        ),
    ):
        bridge.handle(event)
    finished = bridge.finish("success")
    trace = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert finished.status == "success" and not bridge.trace_persistence_failed
    assert "unfinished answer" not in str(finished.selected_messages)
    assert "finished reasoning from failed attempt" not in str(finished.selected_messages)
    old = next(item for item in trace.items if item.item.get("type") == "reasoning")
    replacement = next(item for item in trace.items if item.item.get("type") == "retry")
    assert (old.message_idx, old.item_idx) == (replacement.message_idx, replacement.item_idx)
    assert old.sequence < replacement.sequence
    assert [item.sequence for item in trace.items] == list(range(1, trace.last_sequence + 1))
    events = [item.item for item in trace.items if item.role == "runtime"]
    retry = next(item["data"] for item in events if item["event"] == "model_retry")
    assert retry["failed_request_id"].endswith(":request-1:1") and retry["retry_number"] == 1
    response = next(item["data"] for item in events if item["event"] == "model_response")
    assert response["request_id"].endswith(":request-1:2") and response["usage"]["input_tokens"] == 12
    exported = list(conversation_trace_records(store, turn.session_id, turn.thread_id))
    assert len([item for item in exported if item["type"] == "item"]) == trace.last_sequence


def test_execution_events_capture_modes_tools_compaction_and_controls(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    bridge = bound_bridge(runtime, store, turn)
    initialize_trace(runtime)
    bridge.handle(RuntimeEvent("run_resumed", "user resumed"))
    bridge.handle(
        RuntimeEvent(
            "model_request",
            data={
                "model": "local-test",
                "exchange_id": "exchange-1",
                "request_parameters": {"reasoning_effort": "high"},
            },
        )
    )
    bridge.apply_runtime_config(RuntimeConfigUpdate(running_mode="plan"))
    pending = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert pending.items[-1].item["event"] == "mode_change_requested"
    bridge.handle(RuntimeEvent("assistant_message", data={"message": {"role": "assistant", "content": "ready"}}))
    bridge.handle(
        RuntimeEvent(
            "model_response",
            data={
                "model": "changed-after-request",
                "usage": {"input_tokens": 21, "output_tokens": 4},
            },
        )
    )
    bridge.handle(
        RuntimeEvent(
            "tool_call",
            "read_file",
            {
                "call_id": "tool-1",
                "tool": "read_file",
                "arguments": {"path": "README.md", "api_key": "private"},
            },
        )
    )
    bridge.handle(RuntimeEvent("tool_result", "file contents", {"call_id": "tool-1", "tool": "read_file"}))
    bridge.handle(
        RuntimeEvent("context_compaction_started", data={"trigger": "manual", "estimated_tokens_before": 200})
    )
    bridge.handle(RuntimeEvent("context_compaction_failed", "summary failed", {"trigger": "manual"}))
    bridge.handle(RuntimeEvent("cancelled", "user cancelled", {"stop_reason": "user"}))
    trace = store.load_turn_trace(turn.session_id, turn.id, 0)
    events = [item.item for item in trace.items if item.role == "runtime"]
    by_kind = {item["event"]: item["data"] for item in events}
    mode = by_kind["mode_changed"]
    assert (mode["old_mode"], mode["new_mode"], mode["source"]) == ("agent", "plan", "runtime_config")
    assert mode["requested_at"] <= mode["effective_at"]
    request = by_kind["model_response"]
    assert (request["model"], request["reasoning_effort"], request["status"]) == ("local-test", "high", "success")
    assert request["started_at"] <= request["ended_at"]
    tool = by_kind["tool_result"]
    assert tool["arguments"] == {"path": "README.md", "api_key": "[REDACTED]"}
    assert tool["result"] == "file contents" and tool["started_at"] <= tool["ended_at"]
    compact = by_kind["context_compaction_failed"]
    assert compact["estimated_tokens_before"] == 200 and compact["status"] == "failed"
    assert by_kind["turn_finished"]["status"] == "paused" and "run_resumed" in by_kind
    assert all(item.message_idx is None and item.item_idx is None for item in trace.items if item.role == "runtime")


def test_token_metrics_remain_numeric_without_exposing_credentials() -> None:
    assert turn_trace_audit_value(
        {
            "usage": {"input_tokens": 20, "output_tokens_details": {"reasoning_tokens": 3}},
            "estimated_tokens_after": 10,
            "target_tokens": 100,
            "access_token": "private",
            "headers": {"Authorization": "private"},
        }
    ) == {
        "usage": {"input_tokens": 20, "output_tokens_details": {"reasoning_tokens": 3}},
        "estimated_tokens_after": 10,
        "target_tokens": 100,
        "access_token": "[REDACTED]",
    }


def test_concurrent_repeated_records_receive_distinct_sequences(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    initialize_trace(runtime)
    entry = store.load_turn_trace(turn.session_id, turn.id, 0).items[0]

    def append(_index: int) -> None:
        store.append_turn_trace_item(
            turn.session_id,
            turn.id,
            0,
            message_idx=entry.message_idx,
            item_idx=entry.item_idx,
            role=entry.role,
            item=entry.item,
            completed_at="2026-09-15T12:00:00+00:00",
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(append, range(9)))
    trace = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert [item.sequence for item in trace.items] == list(range(1, 11))
