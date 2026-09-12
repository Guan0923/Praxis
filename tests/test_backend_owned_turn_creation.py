from __future__ import annotations

import re

from fastapi.testclient import TestClient

from backend.api import turn_message_worker as worker_module
from backend.api.app import create_app
from backend.api.routes import turns as turns_module
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.domain import MessageQueueUnavailable
from backend.domain.runtime_state import NodeWriter, RuntimeState
from backend.providers import ModelConfig
from backend.storage.message_queue import MemoryMessageQueue
from backend.storage.sqlite import SQLiteSessionStore


def configure_local_model(state: WebAppState) -> None:
    state.model_config = lambda provider_name=None: ModelConfig(
        api_key="local-test-only",
        base_url="http://127.0.0.1:1",
        model="local-test",
    )


def create_body(sidebar: dict[str, object], text: str = "hello") -> dict[str, object]:
    return {
        "session_id": sidebar["session_id"],
        "thread_id": sidebar["thread_id"],
        "parent_id": "",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def forbid_global_turn_lookup(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("Turn creation must not scan sessions or load full histories")

    monkeypatch.setattr(SQLiteSessionStore, "find_node", fail)
    monkeypatch.setattr(SQLiteSessionStore, "list_sessions", fail)
    monkeypatch.setattr(SQLiteSessionStore, "get_session_summary", fail)
    monkeypatch.setattr(SQLiteSessionStore, "load_nodes", fail)


def test_create_returns_the_persisted_backend_turn_without_global_lookup(tmp_path, monkeypatch) -> None:
    state = WebAppState(tmp_path / "data")
    configure_local_model(state)
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        assert client.post("/api/turns", json={**create_body(sidebar), "id": "frontend-turn"}).status_code == 422
        forbid_global_turn_lookup(monkeypatch)

        response = client.post("/api/turns", json=create_body(sidebar))

        assert response.status_code == 202, response.text
        turn = response.json()
        assert re.fullmatch(r"turn_[0-9a-f]{32}", turn["id"])
        assert turn["session_id"] == sidebar["session_id"]
        assert turn["thread_id"] == sidebar["thread_id"]
        assert turn["status"] == "running"
        assert turn["data"][0][0]["content"][0]["text"] == "hello"
        assert turn["data"][0][1]["role"] == "assistant"

        claimed = state.message_queue.claim_turn_start("test")
        assert claimed is not None
        assert claimed.envelope.target_id == turn["id"]
        assert claimed.envelope.delivery_id == f"turn-start:{turn['id']}"
        store = session_store(state)
        assert store.get_node(sidebar["session_id"], turn["id"]).to_dict() == turn

        state.message_queue.ack(claimed)
        NodeWriter(store).finalize(store.get_node(sidebar["session_id"], turn["id"]), "success")
        child_response = client.post(
            "/api/turns",
            json={**create_body(sidebar, "child"), "parent_id": turn["id"]},
        )
        assert child_response.status_code == 202, child_response.text
        assert child_response.json()["parent_id"] == turn["id"]


def test_worker_adopts_the_created_turn_without_looking_it_up_globally(tmp_path, monkeypatch) -> None:
    state = WebAppState(tmp_path / "data")
    configure_local_model(state)
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        created = client.post("/api/turns", json=create_body(sidebar)).json()
        claimed = state.message_queue.claim_turn_start("test")
        observed: dict[str, object] = {}
        monkeypatch.setattr(
            SQLiteSessionStore,
            "find_node",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("worker used a global Turn lookup")),
        )
        monkeypatch.setattr(worker_module, "_stream_turn", lambda *_args, **kwargs: observed.update(kwargs))

        state.turn_message_worker._start(claimed)

        assert observed["turn_id"] == created["id"]
        assert observed["source_id"] == created["id"]
        assert observed["adopt_existing"] is True
        assert observed["precreated"] is True


def test_sse_uses_the_supplied_session_for_exact_turn_lookup(tmp_path, monkeypatch) -> None:
    state = WebAppState(tmp_path / "data")
    configure_local_model(state)
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        created = client.post("/api/turns", json=create_body(sidebar)).json()
        monkeypatch.setattr(
            SQLiteSessionStore,
            "find_node",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SSE used a global Turn lookup")),
        )
        monkeypatch.setattr(
            turns_module,
            "turn_sse",
            lambda *_args, **_kwargs: iter([f'data: <SSE id="{created["id"]}" type="success"></SSE>\n\n']),
        )

        missing_session = client.get(f"/api/turns/{created['id']}/stream")
        response = client.get(
            f"/api/turns/{created['id']}/stream",
            params={"session_id": sidebar["session_id"]},
        )

        assert missing_session.status_code == 422
        assert response.status_code == 200


def test_enqueue_failure_marks_the_saved_turn_failed(tmp_path) -> None:
    class FailingQueue(MemoryMessageQueue):
        def dispatch_turn_start(self, envelope):
            del envelope
            raise MessageQueueUnavailable("queue stopped")

    state = WebAppState(tmp_path / "data", message_queue=FailingQueue())
    configure_local_model(state)
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()

        response = client.post("/api/turns", json=create_body(sidebar))

        assert response.status_code == 503
        turns = session_store(state).load_nodes(sidebar["session_id"])
        ordinary = [turn for turn in turns if isinstance(turn, RuntimeState)]
        assert len(ordinary) == 1
        assert ordinary[0].status == "failed"
        assert ordinary[0].assistant_items[-1]["error_report"]["type"] == "MessageQueueUnavailable"


def test_fork_rejects_a_frontend_generated_turn_id(tmp_path) -> None:
    state = WebAppState(tmp_path / "data")
    configure_local_model(state)
    state.turn_message_worker.close()
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        created = client.post("/api/turns", json=create_body(sidebar)).json()

        response = client.post(f"/api/turns/{created['id']}/fork", json={"id": "frontend-fork"})

        assert response.status_code == 422
