from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.domain.execution_config import RuntimeModelRequest, TurnExecutionConfig
from backend.domain.input_message import FileReference, InputMessage
from backend.domain.message_queue import DeliveryConflict, MessageEnvelope, QueuedMessage, TurnStart
from backend.planning import RuleBasedPlanner
from backend.providers import ModelConfig
from backend.runtime import AgentRunner
from backend.runtime.conversation.service import ConversationService
from backend.runtime.conversation.steering import apply_steering, collect_steering
from backend.storage.message_queue import MemoryMessageQueue, TurnMailbox
from backend.storage.sqlite import SQLiteSessionStore
from backend.tools import ToolRegistry


def test_nested_input_is_immutable_and_output_copies_do_not_change_delivery():
    source = [{"source": "workspace", "path": "workspace:notes.txt", "display_path": "notes.txt"}]
    message = InputMessage.from_input("inspect", source)
    source[0]["path"] = "workspace:changed.txt"
    queue = MemoryMessageQueue()
    queue.create(QueuedMessage("draft", "thread", message))
    delivery = queue.dispatch(
        delivery_id="delivery", message_ids=["draft"], session_id="session", thread_id="thread", turn_id="turn"
    )
    claimed = queue.claim("turn", "reader")
    assert claimed.envelope.message is delivery.message
    with pytest.raises(FrozenInstanceError):
        delivery.message.references[0].path = "workspace:changed.txt"
    wire = delivery.message.to_item()
    wire["references"][0]["path"] = "workspace:changed.txt"
    assert claimed.envelope.message.references[0] == FileReference("workspace:notes.txt", "workspace", "notes.txt")
    with pytest.raises(TypeError):
        InputMessage("mutable", [FileReference("workspace:notes.txt")])


def test_message_edits_replace_values_without_changing_previous_input():
    queue = MemoryMessageQueue()
    original = queue.create(QueuedMessage("draft", "thread", InputMessage("first")))[0]
    updated = queue.update("thread", "draft", message=InputMessage("second"))
    assert original.message.text == "first"
    assert updated.message.text == "second"
    envelope = queue.dispatch(delivery_id="d", message_ids=["draft"], session_id="s", thread_id="thread", turn_id="t")
    assert envelope.message.text == "second"


def test_startup_config_and_message_pass_through_worker_without_revalidation(tmp_path, monkeypatch):
    import backend.api.turn_message_worker as worker_module

    state = WebAppState(tmp_path / "data")
    state.model_config = lambda provider_name=None: ModelConfig(
        api_key="local-test-only",
        base_url="http://127.0.0.1:1",
        model="local-test",
    )
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        response = client.post(
            "/api/turns",
            json={
                "session_id": sidebar["session_id"],
                "thread_id": sidebar["thread_id"],
                "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                "model": {
                    "reasoning_effort": "high",
                    "current_model": "local",
                    "context_length": 4096,
                    "output_length": 128,
                    "thinking": "enable",
                    "temperature": 0.2,
                },
            },
        )
        assert response.status_code == 202
        created_id = response.json()["id"]
        claimed = state.message_queue.claim_turn_start("test")
        assert claimed.envelope.target_id == created_id
        observed = {}
        monkeypatch.setattr(worker_module, "_stream_turn", lambda *args, **kwargs: observed.update(kwargs))

        def reject_conversion(*args, **kwargs):
            raise AssertionError("Internal startup attempted a configuration round trip")

        monkeypatch.setattr(TurnExecutionConfig, "model_validate", reject_conversion)
        monkeypatch.setattr(RuntimeModelRequest, "model_dump", reject_conversion)
        state.turn_message_worker._start(claimed)
        assert observed["message"] is claimed.envelope.message
        assert observed["config"] is claimed.envelope.start.config
        with pytest.raises(ValidationError):
            observed["config"].model.temperature = 0.7


def test_delivery_conflict_compares_typed_content_and_ignores_claim_attempts():
    queue = MemoryMessageQueue()
    message = MessageEnvelope(
        "d",
        "user",
        "thread",
        "turn_start",
        "turn",
        "s",
        "thread",
        InputMessage("original"),
        ("d",),
        start=TurnStart("create", TurnExecutionConfig()),
    )
    queue.dispatch_turn_start(message)
    queue.dispatch_turn_start(replace(message, attempts=3))
    with pytest.raises(DeliveryConflict):
        queue.dispatch_turn_start(replace(message, message=InputMessage("different")))
    with pytest.raises(ValueError):
        TurnStart("unsupported", TurnExecutionConfig())


def test_real_sqlite_failure_does_not_acknowledge_mailbox(tmp_path: Path, monkeypatch):
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    service = ConversationService(AgentRunner(RuleBasedPlanner(), ToolRegistry()), store)
    service.run_task("initial", mode="agent")
    runtime = service.runtime
    queue = MemoryMessageQueue()
    queue.create(QueuedMessage("draft", runtime.state.thread_id, InputMessage("follow up")))
    queue.dispatch(
        delivery_id="sqlite-follow-up",
        message_ids=["draft"],
        session_id=runtime.state.session_id,
        thread_id=runtime.state.thread_id,
        turn_id=runtime.run.turn_id,
    )
    mailbox = TurnMailbox(queue, runtime.run.turn_id, "test")
    runtime.services.steering = mailbox.take
    update = collect_steering(runtime)
    connection = store._connection

    @contextmanager
    def readonly(*args, **kwargs):
        with connection(*args, **kwargs) as db:
            if kwargs.get("write"):
                db.execute("PRAGMA query_only=ON")
            yield db

    monkeypatch.setattr(store, "_connection", readonly)
    with pytest.raises(sqlite3.OperationalError):
        apply_steering(runtime, update, phase="test")
    assert queue.list(runtime.state.thread_id)[0].state == "dispatched"
    assert not store.has_turn_delivery(runtime.state.session_id, "sqlite-follow-up")
    queue.close()


def test_acknowledged_comparison_does_not_retain_evicted_message_body():
    queue = MemoryMessageQueue()
    envelope = MessageEnvelope(
        "evicted",
        "agent",
        "source",
        "thread",
        "target",
        "session",
        "target",
        InputMessage("body-that-must-leave-the-conversation-cache"),
        ("evicted",),
    )
    queue.dispatch_agent(envelope)
    queue.ack(queue.claim_thread("target", "reader"))
    queue.release_thread_cache("target")
    assert "body-that-must-leave-the-conversation-cache" not in repr(queue._receipts)
