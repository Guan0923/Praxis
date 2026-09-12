from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.domain.runtime_state import RuntimeState
from tests.test_view_state_sync import seed


def test_empty_history_has_explicit_null_head(tmp_path):
    state = WebAppState(tmp_path / "data")
    session, _ = seed(state.session_store, 0)
    with TestClient(create_app(state)) as client:
        response = client.get(
            "/api/turns/history", params={"session_id": session.session_id, "thread_id": session.session_id}
        )
        assert response.status_code == 200
        assert response.json() == {"current_turn_id": None, "turns": [], "next_cursor": None, "has_more": False}


def test_all_pages_keep_the_backend_head_and_follow_parents(tmp_path):
    state = WebAppState(tmp_path / "data")
    session, head = seed(state.session_store, 8)
    params = {"session_id": session.session_id, "thread_id": session.session_id}
    with TestClient(create_app(state)) as client:
        page = client.get("/api/turns/history", params=params).json()
        assert page["current_turn_id"] == head.id
        assert page["turns"][-1]["id"] == head.id
        assert len(page["turns"]) == 5
        older = client.get("/api/turns/history", params={**params, "before": page["next_cursor"]}).json()
        assert older["current_turn_id"] == head.id
        assert older["turns"][-1]["id"] == page["turns"][0]["parent_id"]
        assert len(older["turns"]) == 3
        assert older["next_cursor"] is None
        assert not older["has_more"]


def test_fork_history_uses_its_own_head(tmp_path):
    state = WebAppState(tmp_path / "data")
    session, head = seed(state.session_store, 3)
    state.session_store.create_sidebar_thread(
        session_id=session.session_id, thread_id=session.session_id, title="Source"
    )
    with TestClient(create_app(state)) as client:
        response = client.post(f"/api/turns/{head.id}/fork", params={"session_id": session.session_id}, json={})
        assert response.status_code == 201, response.text
        fork = response.json()
        page = client.get(
            "/api/turns/history",
            params={"session_id": session.session_id, "thread_id": fork["sidebar_thread"]["thread_id"]},
        ).json()
        assert page["current_turn_id"] == fork["turn"]["id"]
        assert page["turns"][-1]["id"] == fork["turn"]["id"]


def test_head_and_nodes_are_read_from_one_snapshot(tmp_path, monkeypatch):
    state = WebAppState(tmp_path / "data")
    store = state.session_store
    session, head = seed(store, 2)
    sid = session.session_id
    import sqlite3

    with sqlite3.connect(store.paths.session_db(sid)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    original = store._json_object
    appended = False
    child = RuntimeState.create(
        session_id=sid,
        thread_id=sid,
        parent=head,
        user_content=[{"type": "text", "text": "new"}],
        provider_name="local",
    )

    def read(connection, session_id, namespace, object_id):
        nonlocal appended
        if not appended and namespace == "runtime_node":
            appended = True
            store.create_node(child)
        return original(connection, session_id, namespace, object_id)

    monkeypatch.setattr(store, "_json_object", read)
    page = store.load_turn_page(sid, sid)
    assert page.current_turn_id == head.id
    assert page.turns[-1].id == head.id
    assert store.get_runtime_thread(sid, sid).current_turn_id == child.id
