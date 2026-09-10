from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.jsonl import jsonl_download
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.domain.runtime_state import RuntimeState
from backend.domain.turn_trace import TurnTrace, TurnTraceContext, TurnTraceItem
from backend.runtime.conversation.trace import conversation_trace_records
from benchmarks.resources import ResourceStore
from benchmarks.service import BatchRun, BenchmarkService, TaskRun
from benchmarks.tasks import ALL_TASKS
from tests.test_benchmark_service import result_for

LONG_RESULT = "中文工具结果\n" * 20_000


def seed_thread(web: WebAppState) -> tuple[str, list[RuntimeState]]:
    store = session_store(web)
    session_id = store.create_session("Trace export acceptance").session_id
    store.create_sidebar_thread(session_id=session_id, thread_id=session_id, title="Trace export acceptance")
    parent = store.ensure_root_node(session_id)
    turns = []
    for turn_id, timestamp, status in [
        ("turn-export-old", "2026-09-09T00:00:00Z", "success"),
        ("turn-export-b", "2026-09-10T00:00:00Z", "success"),
        ("turn-export-a", "2026-09-10T00:00:00Z", "running"),
    ]:
        versions = [
            [
                {"role": "user", "content": [{"type": "text", "text": text, "status": "success"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "answer", "status": "success"}]},
            ]
            for text in ["HISTORICAL_ONLY", "current question"]
        ]
        turn = RuntimeState.create(
            session_id=session_id,
            thread_id=session_id,
            parent=parent,
            id=turn_id,
            user_content="current question",
            provider_name="local-test",
            data=versions,
            timestamp=timestamp,
        )
        turn.current_data_idx = 1
        store.create_node(turn)
        turn.status = status
        store.update_node(turn)
        turns.append(turn)
        parent = turn
        if turn_id == "turn-export-a":
            continue
        for data_idx in range(2):
            context = TurnTraceContext(
                "HISTORICAL_ONLY" if data_idx == 0 else "system\n中文上下文",
                [],
                [],
                timestamp,
            )
            items = [
                TurnTraceItem(
                    2,
                    1,
                    0,
                    "assistant",
                    {
                        "type": "tool_result",
                        "call_id": "call-export",
                        "tool": "read_file",
                        "status": "success",
                        "content": LONG_RESULT,
                        "headers": {"Authorization": "[REDACTED]"},
                    },
                    timestamp,
                ),
                TurnTraceItem(1, 0, 0, "user", versions[data_idx][0]["content"][0], timestamp),
            ]
            store.initialize_turn_trace(
                session_id, TurnTrace(turn.id, session_id, data_idx, context, items, 2, timestamp)
            )
    return session_id, turns


def seed_benchmark(web: WebAppState, *, session_id: str | None = None) -> tuple[str, str, list[dict]]:
    task = ALL_TASKS[0]
    result = result_for(task)
    if session_id is None:
        session_id, _ = seed_thread(web)
    result.trace = list(conversation_trace_records(session_store(web), session_id, session_id))
    service = BenchmarkService(
        web.job_registry, web.paths.root / "benchmark-tests", resources=ResourceStore(web.paths.root / "resources")
    )
    item = TaskRun("test-export", task, "llm", None, status="completed", result=result)
    service._runs["run-export"] = BatchRun("run-export", [item])
    web.benchmark_service = service
    return "run-export", item.id, result.trace


@pytest.fixture
def trace_app(tmp_path):
    web = WebAppState(tmp_path / "web")
    session_id, turns = seed_thread(web)
    with TestClient(create_app(web)) as client:
        yield web, client, session_id, turns


def test_thread_export_current_versions_order_and_complete_content(trace_app):
    web, client, session_id, turns = trace_app
    store = session_store(web)
    foreign_session = store.create_session("not exported").session_id
    foreign = RuntimeState.create(
        session_id=foreign_session,
        thread_id=foreign_session,
        user_content="OTHER_SESSION",
        parent=store.ensure_root_node(foreign_session),
    )
    store.create_node(foreign)
    sibling = RuntimeState.create(
        session_id=session_id, thread_id="other-thread", user_content="OTHER_THREAD", parent=turns[0]
    )
    store.create_node(sibling)
    response = client.get("/api/turns/trace/export", params={"session_id": session_id, "thread_id": session_id})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-ndjson; charset=utf-8"
    assert response.headers["content-disposition"] == f'attachment; filename="thread-{session_id}-trace.jsonl"'
    assert response.headers["cache-control"] == "no-store"
    rows = [json.loads(line) for line in response.content.decode("utf-8").splitlines()]
    assert [(row["turn_id"], row["type"]) for row in rows] == [
        ("turn-export-old", "context"),
        ("turn-export-old", "item"),
        ("turn-export-old", "item"),
        ("turn-export-a", "context"),
        ("turn-export-b", "context"),
        ("turn-export-b", "item"),
        ("turn-export-b", "item"),
    ]
    assert all(row["session_id"] == row["thread_id"] == session_id and row["data_idx"] == 1 for row in rows)
    assert rows[3]["data"] is None
    assert rows[0]["data"]["system_message"] == "system\n中文上下文"
    assert [row["data"]["sequence"] for row in rows if row["type"] == "item"] == [1, 2, 1, 2]
    assert rows[2]["data"]["item"]["content"] == LONG_RESULT
    assert rows[2]["data"]["item"]["headers"]["Authorization"] == "[REDACTED]"
    assert "HISTORICAL_ONLY" not in response.text
    assert response.content.endswith(b"\n")
    run_id, task_id, _ = seed_benchmark(web, session_id=session_id)
    benchmark = client.get(f"/benchmark/runs/{run_id}/tasks/{task_id}/trace/export")
    assert benchmark.content == response.content


def test_thread_export_empty_missing_and_unavailable(trace_app):
    web, client, session_id, _ = trace_app
    store = session_store(web)
    empty = store.create_session("empty").session_id
    assert client.get("/api/turns/trace/export", params={"session_id": empty, "thread_id": empty}).content == b""
    for query in [
        {"session_id": session_id, "thread_id": "missing"},
        {"session_id": "missing", "thread_id": session_id},
        {"session_id": empty, "thread_id": session_id},
    ]:
        assert client.get("/api/turns/trace/export", params=query).status_code == 404
    for state in ["archived_at", "deleted_at"]:
        store.update_sidebar_thread(session_id, **{state: "2026-09-10T00:00:00Z"})
        assert (
            client.get(
                "/api/turns/trace/export", params={"session_id": session_id, "thread_id": session_id}
            ).status_code
            == 409
        )


def test_benchmark_export_preserves_records_empty_and_missing(trace_app):
    web, client, _, _ = trace_app
    run_id, task_id, events = seed_benchmark(web)
    url = f"/benchmark/runs/{run_id}/tasks/{task_id}/trace/export"
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == f'attachment; filename="benchmark-{run_id}-{task_id}-trace.jsonl"'
    assert [json.loads(line) for line in response.content.decode("utf-8").splitlines()] == events
    assert response.headers["cache-control"] == "no-store"
    item = web.benchmark_service._runs[run_id].tasks[0]
    item.result = replace(item.result, trace=[])
    for status in ["completed", "failed", "cancelled"]:
        item.status = status
        assert client.get(url).content == b""
    assert client.get(url.replace(task_id, "missing")).status_code == 404
    assert client.get(url.replace(run_id, "missing")).status_code == 404


def test_jsonl_filename_cannot_inject_headers_or_paths():
    response = jsonl_download([], 'thread-../../中文"\r\nInjected: yes-trace.jsonl')
    assert (
        response.headers["content-disposition"] == 'attachment; filename="thread-.._..______Injected__yes-trace.jsonl"'
    )
