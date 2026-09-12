from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.chat import routes as chat_routes
from backend.api.chat import streaming as chat_streaming
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.domain import AssistantMessage, DeliveryConflict, MessageEnvelope, QueuedMessage
from backend.domain.execution_config import TurnExecutionConfig
from backend.domain.input_message import InputMessage
from backend.domain.message_queue import TurnStart
from backend.domain.runtime_state import RuntimeState
from backend.providers import ModelConfig
from backend.runtime import AgentApplication, AgentRunner
from backend.storage.message_queue import MemoryMessageQueue
from backend.tools import ToolRegistry


def configure_local_model(state: WebAppState) -> None:
    state.model_config = lambda provider_name=None: ModelConfig(
        api_key="local-test-only",
        base_url="http://127.0.0.1:1",
        model="local-test",
    )


def test_queued_message_api_is_ordered_idempotent_and_restricts_dispatched_mutations(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        thread_id = sidebar["thread_id"]
        readme = state.session_workspace(sidebar["session_id"]) / "README.md"
        readme.write_text("queued reference", encoding="utf-8")
        first_id = str(uuid4())
        second_id = str(uuid4())
        first = {
            "id": first_id,
            "content": "first",
            "references": [{"source": "workspace", "path": str(readme.resolve()), "display_path": "forged.md"}],
        }
        assert client.post(f"/api/sidebar-threads/{thread_id}/queued-messages", json=first).status_code == 201
        assert client.post(f"/api/sidebar-threads/{thread_id}/queued-messages", json=first).status_code == 200
        assert (
            client.post(
                f"/api/sidebar-threads/{thread_id}/queued-messages",
                json={**first, "content": "conflict"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/sidebar-threads/{thread_id}/queued-messages",
                json={"id": second_id, "content": "second", "references": []},
            ).status_code
            == 201
        )
        updated = client.patch(
            f"/api/sidebar-threads/{thread_id}/queued-messages/{second_id}",
            json={"content": "second edited", "references": []},
        )
        assert updated.status_code == 200
        assert [item["content"] for item in client.get(f"/api/sidebar-threads/{thread_id}/queued-messages").json()] == [
            "first",
            "second edited",
        ]

        state.message_queue.dispatch(
            delivery_id="delivery-api",
            message_ids=[first_id],
            session_id=sidebar["session_id"],
            thread_id=thread_id,
            turn_id="turn-api",
        )
        assert (
            client.patch(
                f"/api/sidebar-threads/{thread_id}/queued-messages/{first_id}",
                json={"content": "late", "references": []},
            ).status_code
            == 409
        )
        assert client.delete(f"/api/sidebar-threads/{thread_id}/queued-messages/{first_id}").status_code == 409
        assert client.delete(f"/api/sidebar-threads/{thread_id}/queued-messages/{second_id}").status_code == 204


def test_create_turn_accepts_exactly_one_message_source_and_enqueues_queued_delivery(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    configure_local_model(state)
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        message_id = str(uuid4())
        assert (
            client.post(
                f"/api/sidebar-threads/{sidebar['thread_id']}/queued-messages",
                json={"id": message_id, "content": "queued turn", "references": []},
            ).status_code
            == 201
        )
        base = {
            "session_id": sidebar["session_id"],
            "thread_id": sidebar["thread_id"],
        }
        assert client.post("/api/turns", json=base).status_code == 422
        assert (
            client.post(
                "/api/turns",
                json={
                    **base,
                    "message": {"role": "user", "content": [{"type": "text", "text": "duplicate"}]},
                    "queued_delivery": {"message_ids": [message_id]},
                },
            ).status_code
            == 422
        )
        response = client.post(
            "/api/turns",
            json={
                **base,
                "queued_delivery": {"message_ids": [message_id]},
            },
        )
        assert response.status_code == 202
        created = response.json()
        assert created["id"].startswith("turn_")
        assert created["status"] == "running"
        assert created["data"][0][0]["content"][0]["text"] == "queued turn"
        initial = state.message_queue.claim_turn_start("test")
        assert initial is not None
        assert initial.envelope.target_id == created["id"]
        assert initial.envelope.message.text == "queued turn"
        assert tuple(initial.envelope.message.reference_dicts()) == ()
    assert initial.envelope.delivery_id == f"turn-start:{created['id']}"


def test_create_turn_validates_and_normalizes_absolute_file_references(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    configure_local_model(state)
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        workspace = state.session_workspace(sidebar["session_id"])
        referenced = workspace / "docs" / "guide.md"
        referenced.parent.mkdir()
        referenced.write_text("guide", encoding="utf-8")
        base = {
            "session_id": sidebar["session_id"],
            "thread_id": sidebar["thread_id"],
            "parent_id": "",
        }
        accepted = client.post(
            "/api/turns",
            json={
                **base,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "inspect",
                            "references": [
                                {
                                    "source": "workspace",
                                    "path": str(referenced.resolve()),
                                    "display_path": "forged.md",
                                }
                            ],
                        }
                    ],
                },
            },
        )
        assert accepted.status_code == 202, accepted.text
        claimed = state.message_queue.claim_turn_start("test-reference")
        assert claimed is not None
        assert tuple(claimed.envelope.message.reference_dicts()) == (
            {
                "source": "workspace",
                "path": "workspace:docs/guide.md",
                "display_path": "workspace:docs/guide.md",
            },
        )

        rejected = client.post(
            "/api/turns",
            json={
                **base,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "inspect",
                            "references": [
                                {
                                    "source": "workspace",
                                    "path": "project:docs/guide.md",
                                    "display_path": "docs/guide.md",
                                }
                            ],
                        }
                    ],
                },
            },
        )
        assert rejected.status_code == 422
        assert "前缀与来源不一致" in rejected.json()["detail"]


def test_create_turn_keeps_accepted_delivery_pending_until_worker_admission(tmp_path: Path) -> None:
    from backend.storage.message_queue import MemoryMessageQueue

    queue = MemoryMessageQueue()
    state = WebAppState(tmp_path / "web", message_queue=queue)
    configure_local_model(state)
    state.turn_message_worker.close()
    with TestClient(create_app(state), raise_server_exceptions=False) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        message_id = str(uuid4())
        queue.create(QueuedMessage(message_id, sidebar["thread_id"], InputMessage.from_input("retry later")))
        response = client.post(
            "/api/turns",
            json={
                "session_id": sidebar["session_id"],
                "thread_id": sidebar["thread_id"],
                "queued_delivery": {
                    "message_ids": [message_id],
                },
            },
        )

        assert response.status_code == 202
        created_id = response.json()["id"]
        assert [(item.id, item.state) for item in queue.list(sidebar["thread_id"])] == [(message_id, "dispatched")]
        claimed = queue.claim_turn_start("replacement")
        assert claimed is not None and claimed.envelope.delivery_id == f"turn-start:{created_id}"
    assert queue.list(sidebar["thread_id"]) == []


@pytest.mark.parametrize("operation", ["rewind"])
def test_turn_start_worker_acknowledges_permanent_invalid_command(tmp_path: Path, operation: str) -> None:
    from backend.storage.message_queue import MemoryMessageQueue

    queue = MemoryMessageQueue()
    state = WebAppState(tmp_path / "web", message_queue=queue)
    state.turn_message_worker.close()
    try:
        with TestClient(create_app(state)) as client:
            sidebar = client.post("/api/sidebar-threads", json={}).json()
            envelope = MessageEnvelope(
                "delivery-invalid-command",
                "user",
                sidebar["thread_id"],
                "turn_start",
                "turn-invalid-command",
                sidebar["session_id"],
                sidebar["thread_id"],
                InputMessage.from_input("invalid command", []),
                ("delivery-invalid-command",),
                start=TurnStart(operation, TurnExecutionConfig(**{}), parent_id=""),
            )
            queue.dispatch_turn_start(envelope)
            claimed = queue.claim_turn_start("test-worker")
            assert claimed is not None

            state.turn_message_worker._start(claimed)

            assert queue.claim_turn_start("replacement") is None
            terminal = state.runtime_event_stream.latest_turn_event("turn-invalid-command")
            assert terminal is not None
            assert terminal.payload["type"] == "turn.terminal"
            assert terminal.payload["terminal_type"] == "failed"
    finally:
        state.close()


def test_create_turn_fails_closed_when_message_queue_is_unavailable(tmp_path: Path) -> None:
    from backend.domain import MessageQueueUnavailable
    from backend.storage.message_queue import MemoryMessageQueue

    class UnavailableQueue(MemoryMessageQueue):
        def dispatch_turn_start(self, envelope):
            del envelope
            raise MessageQueueUnavailable("message_queue_unavailable")

    state = WebAppState(tmp_path / "web", message_queue=UnavailableQueue())
    configure_local_model(state)
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        response = client.post(
            "/api/turns",
            json={
                "session_id": sidebar["session_id"],
                "thread_id": sidebar["thread_id"],
                "parent_id": "",
                "message": {"role": "user", "content": [{"type": "text", "text": "keep composer"}]},
            },
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "message_queue_unavailable"


@pytest.fixture
def memory_queue():
    queue = MemoryMessageQueue()
    yield queue
    queue.close()


def test_memory_side_chat_runs_while_main_turn_is_running(
    tmp_path: Path, monkeypatch, memory_queue: MemoryMessageQueue
) -> None:
    main_started = threading.Event()
    release_main = threading.Event()
    side_started = threading.Event()
    completions: list[tuple[str, str, str]] = []
    closed_applications: list[object] = []
    terminal_observations: list[tuple[bool, bool, bool]] = []
    executions: list[str] = []

    class LocalPlanner:
        name = "thread-isolation-test"

        def decide(self, runtime):
            executions.append(runtime.run.task)
            if runtime.run.provenance.trigger == "resume":
                unregister = runtime.services.register_operation_abort(lambda: None)
                unregister()
                assert runtime.services.operation_interrupted() is False
            if runtime.run.task == "main":
                main_started.set()
                if not release_main.wait(90):
                    raise TimeoutError("Main test turn was not released.")
            if runtime.run.task == "pause side" and runtime.run.provenance.trigger != "resume":
                side_started.set()
                while not runtime.services.suspend_requested() and not release_main.is_set():
                    sleep(0.01)
            if runtime.run.task == "fail side":
                raise RuntimeError("Expected local test failure.")
            return AssistantMessage(content=f"done: {runtime.run.task}")

    state = WebAppState(tmp_path / "web", message_queue=memory_queue)
    configure_local_model(state)

    class LocalApplication(AgentApplication):
        def close(self):
            super().close()
            closed_applications.append(self)

    def application(_state, **kwargs):
        store = session_store(state)
        return LocalApplication(
            AgentRunner(LocalPlanner(), ToolRegistry(kwargs["workspace"]), checkpoints=store), store
        )

    monkeypatch.setattr(chat_routes, "build_local_application", application)
    publish_terminal = chat_streaming.publish_runtime_terminal

    def observe_terminal(web, **kwargs):
        runtime = session_store(web).load_runtime(kwargs["session_id"], thread_id=kwargs["thread_id"])
        terminal_observations.append(
            (
                runtime.status == "idle",
                kwargs["thread_id"] not in web.active_runtime_stream_locks["keys"],
                len(closed_applications) > len(completions),
            )
        )
        publish_terminal(web, **kwargs)
        completions.append((kwargs["turn_id"], kwargs["terminal_type"], kwargs["message"]))

    monkeypatch.setattr(chat_streaming, "publish_runtime_terminal", observe_terminal)

    def send(client, sidebar, prompt, parent_id="", thread_id=None):
        response = client.post(
            "/api/turns",
            json={
                "session_id": sidebar["session_id"],
                "thread_id": thread_id or sidebar["thread_id"],
                "parent_id": parent_id,
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            },
        )
        assert response.status_code == 202, response.text
        return response.json()["id"]

    def finished(store, turn_id, status="success", *, after=0):
        deadline = monotonic() + 10
        while monotonic() < deadline:
            turn = store.find_node(turn_id)
            if isinstance(turn, RuntimeState) and any(item[0] == turn_id for item in completions[after:]):
                assert turn.status == status, turn.data
                return turn
            sleep(0.02)
        pytest.fail(f"Turn did not finish: {turn_id}")

    with TestClient(create_app(state)) as client:
        try:
            sidebar = client.post("/api/sidebar-threads", json={}).json()
            store = session_store(state)
            turn_seed = send(client, sidebar, "seed")
            finished(store, turn_seed)
            turn_main = send(client, sidebar, "main", turn_seed)
            assert main_started.wait(10)
            response = client.post(
                f"/api/right-panel/{sidebar['session_id']}/side-chats",
                json={"source_turn_id": turn_main},
            )
            assert response.status_code == 201, response.text
            window = response.json()["window"]
            turn_side = send(client, sidebar, "side", window["anchor_turn_id"], window["thread_id"])
            finished(store, turn_side)
            assert store.get_node(sidebar["session_id"], turn_main).status == "running"
            turn_side_next = send(client, sidebar, "side next", turn_side, window["thread_id"])
            finished(store, turn_side_next)

            before = len(completions)
            rewound = client.post(
                f"/api/turns/{turn_side_next}/rewind",
                json={
                    "message": {"role": "user", "content": [{"type": "text", "text": "edited side"}]},
                },
            )
            assert rewound.status_code == 202, rewound.text
            assert len(finished(store, turn_side_next, after=before).data) == 2

            turn_side_pause = send(client, sidebar, "pause side", turn_side_next, window["thread_id"])
            assert side_started.wait(10)
            assert client.post(f"/api/turns/{turn_side_pause}/pause").status_code == 200
            finished(store, turn_side_pause, "paused")
            before = len(completions)
            resumed = client.post(f"/api/turns/{turn_side_pause}/resume", json={})
            assert resumed.status_code == 202, resumed.text
            finished(store, turn_side_pause, after=before)

            turn_side_fail = send(client, sidebar, "fail side", turn_side_pause, window["thread_id"])
            finished(store, turn_side_fail, "failed")
            assert store.load_runtime(sidebar["session_id"]).status == "running"
            assert store.load_runtime(sidebar["session_id"], thread_id=window["thread_id"]).status == "idle"

            forked = client.post(f"/api/turns/{turn_seed}/fork", json={})
            assert forked.status_code == 201, forked.text
            fork_anchor = forked.json()["turn"]["id"]
            branch = forked.json()["sidebar_thread"]
            turn_fork_next = send(client, sidebar, "fork next", fork_anchor, branch["thread_id"])
            finished(store, turn_fork_next)
            assert store.load_runtime(sidebar["session_id"]).current_run.task == "main"
        finally:
            release_main.set()
        finished(store, turn_main)
        turn_main_next = send(client, sidebar, "main next", turn_main)
        finished(store, turn_main_next)
        assert all(all(observation) for observation in terminal_observations), terminal_observations


def test_memory_dispatch_claim_ack_and_receipt_replay(memory_queue: MemoryMessageQueue) -> None:
    thread_id = "thread-real"
    reference = {"source": "project", "path": "project:a", "display_path": "a"}
    memory_queue.create(QueuedMessage("one", thread_id, InputMessage.from_input("one", (reference,))))
    memory_queue.create(QueuedMessage("two", thread_id, InputMessage.from_input("two", (reference,))))
    memory_queue.create(QueuedMessage("three", thread_id, InputMessage.from_input("three")))

    first = memory_queue.dispatch(
        delivery_id="delivery-one",
        message_ids=["two", "one"],
        session_id="session-real",
        thread_id=thread_id,
        turn_id="turn-real",
    )
    memory_queue.dispatch(
        delivery_id="delivery-two",
        message_ids=["three"],
        session_id="session-real",
        thread_id=thread_id,
        turn_id="turn-real",
    )
    assert (
        memory_queue.dispatch(
            delivery_id="delivery-one",
            message_ids=["one", "two"],
            session_id="session-real",
            thread_id=thread_id,
            turn_id="turn-real",
        )
        == first
    )
    assert first.source_message_ids == ("one", "two")
    assert first.message.text == "one\n\ntwo"
    assert tuple(first.message.reference_dicts()) == (reference,)

    claimed_first = memory_queue.claim("turn-real", "consumer-a")
    assert claimed_first is not None and claimed_first.envelope.delivery_id == "delivery-one"
    memory_queue.ack(claimed_first)
    claimed_second = memory_queue.claim("turn-real", "consumer-a")
    assert claimed_second is not None and claimed_second.envelope.delivery_id == "delivery-two"
    memory_queue.ack(claimed_second)
    assert memory_queue.list(thread_id) == []
    memory_queue.ack(claimed_second)

    assert (
        memory_queue.dispatch(
            delivery_id="delivery-one",
            message_ids=["one", "two"],
            session_id="session-real",
            thread_id=thread_id,
            turn_id="turn-real",
        )
        == first
    )
    with pytest.raises(DeliveryConflict):
        memory_queue.dispatch(
            delivery_id="delivery-one",
            message_ids=["three"],
            session_id="session-real",
            thread_id=thread_id,
            turn_id="turn-real",
        )


def test_memory_turn_start_xautoclaim_recovers_one_delivery(memory_queue: MemoryMessageQueue) -> None:
    envelope = MessageEnvelope(
        "turn-start-delivery",
        "user",
        "thread-real",
        "turn_start",
        "turn-real",
        "session-real",
        "thread-real",
        InputMessage.from_input("recover once", []),
        ("turn-start-delivery",),
        start=TurnStart("create", TurnExecutionConfig(**{}), parent_id=""),
    )
    memory_queue.dispatch_turn_start(envelope)
    crashed = memory_queue.claim_turn_start("crashed-worker")
    assert crashed is not None and crashed.envelope.attempts == 1
    memory_queue.retry(crashed)

    replacement = memory_queue.claim_turn_start("replacement-worker")
    assert replacement is not None
    assert replacement.stream_id == crashed.stream_id
    assert replacement.envelope.delivery_id == envelope.delivery_id
    assert replacement.envelope.attempts == 2
    memory_queue.ack(replacement)
    assert memory_queue.claim_turn_start("after-ack") is None


def test_memory_turn_start_pending_delivery_does_not_block_another_session(
    memory_queue: MemoryMessageQueue,
) -> None:
    def envelope(delivery_id: str, session_id: str) -> MessageEnvelope:
        return MessageEnvelope(
            delivery_id,
            "user",
            session_id,
            "turn_start",
            f"turn-{delivery_id}",
            session_id,
            session_id,
            InputMessage.from_input(delivery_id, []),
            (delivery_id,),
            start=TurnStart("create", TurnExecutionConfig(**{}), parent_id=""),
        )

    memory_queue.dispatch_turn_start(envelope("first", "session-first"))
    memory_queue.dispatch_turn_start(envelope("second", "session-second"))
    first = memory_queue.claim_turn_start("worker-first")
    second = memory_queue.claim_turn_start("worker-second")

    assert first is not None and first.envelope.session_id == "session-first"
    assert second is not None and second.envelope.session_id == "session-second"
    memory_queue.ack(first)
    memory_queue.ack(second)


def test_memory_agent_thread_dispatch_is_fifo_deduplicated_and_acknowledged(
    memory_queue: MemoryMessageQueue,
) -> None:
    first = MessageEnvelope(
        "agent-one",
        "agent",
        "thread-source",
        "thread",
        "thread-target",
        "session",
        "thread-target",
        InputMessage.from_input("first", []),
        ("agent-one",),
    )
    second = MessageEnvelope(
        "agent-two",
        "agent",
        "thread-source",
        "thread",
        "thread-target",
        "session",
        "thread-target",
        InputMessage.from_input("second", []),
        ("agent-two",),
    )
    assert memory_queue.dispatch_agent(first) == first
    assert memory_queue.dispatch_agent(first) == first
    memory_queue.dispatch_agent(second)
    with pytest.raises(DeliveryConflict):
        memory_queue.dispatch_agent(
            MessageEnvelope(
                "agent-one",
                "agent",
                "thread-source",
                "thread",
                "thread-target",
                "session",
                "thread-target",
                InputMessage.from_input("changed", []),
                ("agent-one",),
            )
        )

    assert memory_queue.peek_thread("thread-target") == first
    claimed_first = memory_queue.claim_thread("thread-target", "crashed-worker")
    assert claimed_first is not None and claimed_first.envelope.message.text == "first"
    assert memory_queue.claim_thread("thread-target", "worker") is None
    memory_queue.retry(claimed_first)
    recovered_first = memory_queue.claim_thread("thread-target", "replacement-worker")
    assert recovered_first is not None and recovered_first.stream_id == claimed_first.stream_id
    assert recovered_first.envelope.delivery_id == "agent-one"
    memory_queue.ack(recovered_first)
    claimed_second = memory_queue.claim_thread("thread-target", "worker")
    assert claimed_second is not None and claimed_second.envelope.message.text == "second"
    memory_queue.ack(claimed_second)
    assert memory_queue.peek_thread("thread-target") is None


def test_memory_assistant_report_stream_is_independent_and_staged(
    memory_queue: MemoryMessageQueue,
) -> None:
    report = MessageEnvelope(
        "agent-report-one",
        "agent",
        "thread-worker",
        "report",
        "thread-parent",
        "session",
        "thread-parent",
        InputMessage.from_input("thread_path: /root/worker\nthread_status: success\ntask_result: done"),
        ("agent-report-one",),
        created_at="2026-08-31T00:00:00+00:00",
    )
    with memory_queue.prepare_reports() as stage:
        stage(report)
        assert memory_queue.claim_report("thread-parent", "early") is None
    assert memory_queue.peek_thread("thread-parent") is None

    claimed = memory_queue.claim_report("thread-parent", "report-consumer")
    assert claimed is not None
    assert claimed.envelope.message.text == report.message.text
    assert memory_queue.claim_report("thread-parent", "second-consumer") is None
    memory_queue.ack(claimed)
    assert memory_queue.claim_report("thread-parent", "after-ack") is None


def test_sqlite_turn_message_delivery_is_idempotent(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
    store = session_store(state)
    store.start_turn(sidebar["session_id"], "run", "start")
    store.append_turn_input(sidebar["session_id"], "run", "redirect", delivery_id="delivery")
    store.append_turn_input(sidebar["session_id"], "run", "redirect", delivery_id="delivery")

    with sqlite3.connect(state.paths.session_db(sidebar["session_id"])) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM json_objects WHERE namespace='turn_message' "
            "AND json_extract(payload_json, '$.delivery_id')='delivery'"
        ).fetchone()[0]
    assert count == 1
