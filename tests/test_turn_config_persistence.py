from types import SimpleNamespace

from backend.api.routes import turns
from backend.configuration import ClientPaths
from backend.domain.runtime_state import NodeWriter, RuntimeState
from backend.storage.sqlite import SQLiteSessionStore


def test_startup_config_patch_preserves_messages_written_after_initial_read(tmp_path, monkeypatch):
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    session = store.create_session("test")
    root = store.ensure_root_node(session.session_id)
    writer = NodeWriter(store, emit=lambda _frame: None)
    original = writer.create(
        RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            parent=root,
            user_content=[{"type": "text", "text": "hello"}],
        )
    )

    def read_before_start(*_args, **_kwargs):
        current = writer.append_message(original, {"role": "developer", "content": []})
        writer.append_message(current, {"role": "assistant", "content": []})
        return original

    monkeypatch.setattr(turns, "session_store", lambda _state: store)
    monkeypatch.setattr(turns, "_turn", read_before_start)
    state = SimpleNamespace(
        active_runtime_bridges={},
        subagent_coordinator=SimpleNamespace(apply_runtime_config=lambda *_args: None),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(web=state)), state=SimpleNamespace())
    turns.patch_turn_config(original.id, turns.TurnConfigPatch(permission_mode="workspace_write"), request)
    writer.append_items(original, [{"type": "text", "text": "hello", "status": "running"}], message_idx=3)

    loaded = store.get_node(session.session_id, original.id)
    assert len(loaded.selected_messages) == 4
    assert loaded.selected_messages[3]["content"][0]["text"] == "hello"
    assert loaded.permission_mode == "workspace_write"
