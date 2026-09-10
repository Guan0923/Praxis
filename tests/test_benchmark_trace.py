"""Canonical benchmark traces through real loopback transport, tools and SQLite."""

from __future__ import annotations

import json
from threading import Event

import pytest

from backend.runtime.application.services import AgentApplication
from backend.runtime.conversation.trace import conversation_trace_records
from benchmarks.runner import run_one_task
from benchmarks.sandbox import Sandbox
from tests.benchmark_local_support import local_model, local_tasks


@pytest.mark.parametrize("outcome", ["completed", "interrupted", "cancelled"])
def test_long_stream_preserves_canonical_items(tmp_path, monkeypatch, local_sandbox_runtime, outcome):
    chunks = tuple(f"Step {index}: examine cancellation and cleanup. " * 8 + "\n" for index in range(80))
    stopped = Event()
    deltas = []
    expected = []
    original_close = AgentApplication.close

    def close(app):
        sessions = app.session_store.list_sessions()
        assert len(sessions) == 1
        session_id = sessions[0].session_id
        expected.extend(conversation_trace_records(app.session_store, session_id, session_id))
        original_close(app)

    monkeypatch.setattr(AgentApplication, "close", close)

    def on_event(event):
        if event.kind == "thinking_delta":
            deltas.append(event.message)
            if outcome == "cancelled":
                stopped.set()

    with local_model(reasoning_chunks=chunks, interrupt_stream=outcome == "interrupted") as (config, calls):
        sandbox = Sandbox(tmp_path / "sandbox", model_config=config)
        sandbox.prepare()
        result = run_one_task(
            local_tasks()[0], planner="llm", sandbox=sandbox, on_event=on_event, cancel_requested=stopped.is_set
        )

    assert len(calls) == 2
    assert result.status == {"completed": "completed", "interrupted": "failed", "cancelled": "cancelled"}[outcome]
    assert result.trace == expected
    assert result.trace[0]["type"] == "context"
    items = [row["data"]["item"] for row in result.trace if row["type"] == "item"]
    reasoning = [item for item in items if item["type"] == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["text"] == "".join(deltas)
    if outcome != "cancelled":
        assert reasoning[0]["text"] == "".join(chunks)
    assert any(item["type"] == "tool_call" for item in items)
    assert any(item["type"] == "tool_result" for item in items)
    encoded = json.dumps(result.trace)
    assert all(key not in encoded for key in ("thinking_delta", "wire_response", "wire_request", "local-dummy-value"))
    assert len(result.trace) < 20
    if outcome == "interrupted":
        assert any(item["type"] == "error" for item in items)


@pytest.mark.parametrize("failure", ["grading", "cleanup", "trace"])
def test_finalization_failures_preserve_existing_trace(tmp_path, monkeypatch, local_sandbox_runtime, failure):
    import benchmarks.runner as runner

    expected = []
    original_close = AgentApplication.close

    def close(app):
        session_id = app.session_store.list_sessions()[0].session_id
        expected.extend(conversation_trace_records(app.session_store, session_id, session_id))
        original_close(app)
        if failure == "cleanup":
            raise RuntimeError("cleanup failed")

    def grading(*_args):
        raise RuntimeError("grading failed")

    def broken_trace(*args):
        yield next(conversation_trace_records(*args))
        raise RuntimeError("trace read failed")

    monkeypatch.setattr(AgentApplication, "close", close)
    if failure == "grading":
        monkeypatch.setattr(runner, "run_checkers", grading)
    elif failure == "trace":
        monkeypatch.setattr(runner, "conversation_trace_records", broken_trace)
    with local_model() as (config, _calls):
        sandbox = Sandbox(tmp_path / "sandbox", model_config=config)
        sandbox.prepare()
        result = run_one_task(local_tasks()[0], planner="llm", sandbox=sandbox)
    assert result.status == "error"
    assert result.failure_phase == failure
    assert failure in result.error
    assert result.trace == (expected[:1] if failure == "trace" else expected)
    assert result.trace
