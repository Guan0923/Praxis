import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.domain.runtime_state import NodeWriter, RuntimeState
from backend.providers import ModelConfig


def test_controls_keep_parent_and_panel_ownership(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "data")
    state.turn_message_worker.close()
    state.model_config = lambda *_args, **_kwargs: ModelConfig("local-test", "http://127.0.0.1:1/v1", "local-test")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        sid = sidebar["session_id"]
        store = state.session_store
        root = store.ensure_root_node(sid)
        writer = NodeWriter(store)
        source = writer.create(RuntimeState.create(session_id=sid, thread_id=sid, parent=root, user_content="first"))
        source = writer.finalize(source, "success")
        fork = client.post(f"/api/turns/{source.id}/fork", json={}).json()
        branch = fork["sidebar_thread"]["thread_id"]
        assert fork["history"]["current_turn_id"] == fork["turn"]["id"]
        main_window = client.post(f"/api/right-panel/{sid}/files", params={"thread_id": sid}).json()["window"]
        branch_window = client.post(f"/api/right-panel/{sid}/files", params={"thread_id": branch}).json()["window"]
        client.patch(f"/api/right-panel/{sid}", params={"thread_id": branch}, json={"width": 700})
        main_panel = client.get(f"/api/right-panel/{sid}", params={"thread_id": sid}).json()
        branch_panel = client.get(f"/api/right-panel/{sid}", params={"thread_id": branch}).json()
        assert ([w["id"] for w in main_panel["windows"]], main_panel["state"]["width"]) == ([main_window["id"]], 420)
        assert ([w["id"] for w in branch_panel["windows"]], branch_panel["state"]["width"]) == (
            [branch_window["id"]],
            700,
        )
        assert (
            client.delete(
                f"/api/right-panel/{sid}/windows/{main_window['id']}", params={"thread_id": branch}
            ).status_code
            == 404
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": sid,
                "thread_id": sid,
                "message": {"role": "user", "content": [{"type": "text", "text": "second"}]},
            },
        )
        assert response.status_code == 202, response.text
        assert response.json()["parent_id"] == source.id


def test_incremental_storage_is_bounded_without_losing_output(tmp_path: Path) -> None:
    from tests.test_incremental_runtime import new_turn

    store, turn = new_turn(tmp_path)
    writer = NodeWriter(store, emit=lambda _frame: None)
    turn = writer.create(turn)
    turn = writer.append_item(turn, {"type": "text", "text": "", "status": "running"})
    for _ in range(270):
        turn = writer.append_text(turn, data_idx=0, item_idx=0, delta="x", persist=True)
    with sqlite3.connect(store.paths.session_db(turn.session_id)) as connection:
        count = connection.execute(
            "SELECT count(*) FROM json_objects WHERE namespace=?", (f"runtime_delta:{turn.id}",)
        ).fetchone()[0]
    assert count < 256
    assert store.get_node(turn.session_id, turn.id).assistant_items[0]["text"] == "x" * 270
