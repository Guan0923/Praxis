from time import monotonic, sleep
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.chat import routes as chat_routes
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.domain.runtime_state import NodeWriter
from backend.planning.rule_based import RuleBasedPlanner
from backend.providers import ModelConfig
from backend.runtime import AgentApplication, AgentRunner, ConversationService
from backend.tools import ToolRegistry


def test_compact_queue_continues_same_turn_and_finished_steer_is_distinct(tmp_path, monkeypatch):
    state = WebAppState(tmp_path / "web")
    state.model_config = lambda provider_name=None: ModelConfig("", "https://example.test/v1", "local")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        session_id = sidebar["session_id"]
        store = session_store(state)
        runner = AgentRunner(RuleBasedPlanner(), ToolRegistry(state.session_workspace(session_id)))
        service = ConversationService(runner, store, session_id=session_id)
        service.run_task("seed", mode="agent")
        source = store.load_nodes(session_id)[-1]
        compact = store.create_compact_turn(source.id, "checkpoint", new_turn_id="turn_compact_queue")
        compact = NodeWriter(store).finalize(compact, "success")
        response = client.post(
            f"/api/turns/{compact.id}/steer",
            json={"delivery_id": "late", "message_ids": ["pending"]},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "turn_finished"
        monkeypatch.setattr(
            chat_routes,
            "build_local_application",
            lambda *args, **kwargs: AgentApplication(runner, session_store(state)),
        )
        message_id = str(uuid4())
        queued = client.post(
            f"/api/sidebar-threads/{compact.thread_id}/queued-messages",
            json={"id": message_id, "content": "continue after compact", "references": []},
        )
        assert queued.status_code == 201
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "thread_id": compact.thread_id,
                "parent_id": compact.id,
                "queued_delivery": {"message_ids": [message_id]},
            },
        )
        assert response.status_code == 202, response.text
        assert response.json()["id"] == compact.id
        deadline = monotonic() + 10
        while monotonic() < deadline:
            result = store.get_node(session_id, compact.id)
            if result.status != "running" and compact.id not in state.active_turn_streams:
                break
            sleep(0.02)
        result = store.get_node(session_id, compact.id)
        assert result.status == "success"
        messages = [entry for entry in result.data[0] if entry["role"] != "developer"]
        assert [entry["role"] for entry in messages] == ["user", "assistant", "user", "assistant"]
        assert messages[1]["content"][0]["type"] == "compaction"
        assert messages[2]["delivery_id"].startswith("turn-start:")
        assert messages[3]["content"]
        assert client.get(f"/api/sidebar-threads/{compact.thread_id}/queued-messages").json() == []
