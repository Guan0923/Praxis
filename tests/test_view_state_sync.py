import asyncio

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.application_sync import CHANNEL, ApplicationSync
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.domain.runtime_state import RuntimeState
from backend.storage.runtime_event_stream import MemoryRuntimeEventStream
from backend.storage.sqlite import SQLiteSessionStore


def seed(store, count=1):
    session = store.create_session("Navigation test")
    parent = store.ensure_root_node(session.session_id)
    for index in range(count):
        turn = RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            parent=parent,
            user_content=[{"type": "text", "text": f"Question {index}"}],
            provider_name="local",
        )
        store.create_node(turn)
        turn.data[0][-1]["content"] = [{"type": "text", "text": f"Answer {index}", "status": "success"}]
        turn.data.append(
            [
                {"role": "user", "content": [{"type": "text", "text": "Alternative question", "status": "success"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Alternative answer", "status": "success"}]},
            ]
        )
        turn.status = "success"
        store.finalize_node(turn)
        parent = turn
    return session, parent


def test_state_and_small_version_response_survive_reopening(tmp_path):
    state = WebAppState(tmp_path / "data")
    session, turn = seed(state.session_store, 7)
    path = f"/api/view-state/{session.session_id}/{session.session_id}"
    with TestClient(create_app(state)) as client:
        assert client.get(path).json()["revision"] == 0
        response = client.patch(
            path,
            json={"draft": "unsent", "reading": {"top": 120, "offset": 4, "messageId": "anchor", "atBottom": False}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 1
        assert client.patch(path, json={"expanded": {"tool": True}}).json()["draft"] == "unsent"
        selected = client.patch(
            f"/api/turns/{turn.id}/current-data", json={"session_id": session.session_id, "current_data_idx": 1}
        )
        assert selected.status_code == 200, selected.text
        assert set(selected.json()) == {"id", "session_id", "thread_id", "current_data_idx", "revision"}
        assert len(selected.content) < 500
        assert state.session_store.get_node(session.session_id, turn.id).current_data_idx == 1
        events = state.runtime_event_stream.read_thread(CHANNEL, "0", block_ms=0)
        assert any(item.payload["type"] == "view.changed" for item in events)
        assert any(item.payload["type"] == "version.changed" for item in events)
        history = client.get(
            "/api/turns/history", params={"session_id": session.session_id, "thread_id": session.session_id, "limit": 5}
        ).json()
        assert len(history["turns"]) == 5
        assert history["has_more"]
    reopened = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    saved = reopened.get_view_state(session.session_id, session.session_id)
    assert saved["draft"] == "unsent"
    assert saved["expanded"] == {"tool": True}
    assert saved["reading"]["messageId"] == "anchor"


def test_reject_unknown_thread_and_bad_state(tmp_path):
    state = WebAppState(tmp_path / "data")
    session, _ = seed(state.session_store)
    with TestClient(create_app(state)) as client:
        assert client.get(f"/api/view-state/{session.session_id}/missing").status_code == 404
        path = f"/api/view-state/{session.session_id}/{session.session_id}"
        assert client.patch(path, json={"revision": 99}).status_code == 422
        assert client.patch(path, json={"uploads": [{"status": "uploading"}]}).status_code == 422
        assert (
            client.get(f"/api/conversation-target/{session.session_id}/wrong/{session.session_id}").status_code == 404
        )


def test_sync_replay_and_restart_reset():
    class Request:
        async def is_disconnected(self):
            return False

    async def check():
        stream = MemoryRuntimeEventStream()
        sync = ApplicationSync(stream)
        sync.publish("catalog.changed")
        first = sync.events(None, Request())
        reset = await anext(first)
        assert '"sync.reset"' in reset
        cursor = reset.splitlines()[0].removeprefix("id: ")
        sync.publish("session.changed", session_id="new")
        await first.aclose()
        replay = sync.events(cursor, Request())
        assert '"session.changed"' in await anext(replay)
        assert '"sync.ready"' in await anext(replay)
        await replay.aclose()
        restarted = ApplicationSync(MemoryRuntimeEventStream()).events(cursor, Request())
        assert '"sync.reset"' in await anext(restarted)
        await restarted.aclose()

    asyncio.run(check())


def test_production_deep_links_do_not_mask_missing_resources(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>Praxis</html>", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_FRONTEND_DIST", str(dist))
    with TestClient(create_app(WebAppState(tmp_path / "data"))) as client:
        for path in ("/chat/s/t", "/chat/s/t/agent/a", "/benchmark", "/trash"):
            response = client.get(path)
            assert response.status_code == 200
            assert "<html>Praxis" in response.text
        assert client.get("/api/not-found").status_code == 404
        assert client.get("/missing.js").status_code == 404


def test_repeated_panel_read_does_not_publish_a_change(tmp_path):
    state = WebAppState(tmp_path / "data")
    session, _ = seed(state.session_store)
    current = state.session_store.get_right_panel_state(session.session_id)
    cursor = state.runtime_event_stream.latest_thread_id(CHANNEL)
    state.session_store.save_right_panel_state(session.session_id, width=current.width)
    assert state.runtime_event_stream.latest_thread_id(CHANNEL) == cursor
    state.session_store.save_right_panel_state(session.session_id, width=current.width + 1)
    events = state.runtime_event_stream.read_thread(CHANNEL, cursor, block_ms=0)
    assert [event.payload["type"] for event in events] == ["panel.changed"]


def test_expired_sync_position_requires_snapshot(monkeypatch):
    monkeypatch.setattr("backend.storage.runtime_event_stream.EVENT_STREAM_MAXLEN", 2)

    class Request:
        async def is_disconnected(self):
            return False

    async def check():
        sync = ApplicationSync(MemoryRuntimeEventStream())
        for _ in range(4):
            sync.publish("catalog.changed")
        events = sync.events(f"{sync.epoch}:1", Request())
        assert '"reason": "expired"' in await anext(events)
        await events.aclose()

    asyncio.run(check())
