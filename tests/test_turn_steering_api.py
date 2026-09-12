from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.domain import QueuedMessage
from backend.domain.input_message import InputMessage
from backend.domain.runtime_state import RuntimeState
from backend.storage.message_queue import MemoryMessageQueue, TurnMailbox


def test_turn_mailbox_consumes_one_fifo_delivery_per_boundary() -> None:
    queue = MemoryMessageQueue()
    for message_id, content in (("first", "one"), ("second", "two")):
        queue.create(QueuedMessage(message_id, "thread", InputMessage.from_input(content)))
        queue.dispatch(
            delivery_id=f"delivery-{message_id}",
            message_ids=[message_id],
            session_id="session",
            thread_id="thread",
            turn_id="turn",
        )
    inbox = TurnMailbox(queue, "turn", "worker")

    first = inbox.take()[0]
    assert first.delivery_id == "delivery-first"
    assert first.message.text == "one"
    first.acknowledge()
    second = inbox.take()[0]
    assert second.delivery_id == "delivery-second"
    assert second.message.text == "two"
    second.acknowledge()
    assert inbox.take() == []
    inbox.close()


@pytest.mark.parametrize("execution_id", ["turn_running", "original_plan_turn"])
def test_steer_endpoint_accepts_only_an_active_running_turn_and_normalizes_references(
    tmp_path: Path, execution_id: str
) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        workspace = state.session_workspace(sidebar["session_id"])
        project_file = workspace / "README.md"
        project_file.write_text("project", encoding="utf-8")
        upload_file = state.paths.session_uploads(sidebar["session_id"]) / "notes.txt"
        upload_file.write_text("upload", encoding="utf-8")
        store = session_store(state)
        turn = RuntimeState.create(
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            id="turn_running",
            parent=store.ensure_root_node(sidebar["session_id"]),
            user_content=[{"type": "text", "text": "start"}],
            user="",
            provider_name="local",
        )
        store.create_node(turn)
        state.active_turn_streams = {turn.id: SimpleNamespace(turn_id=execution_id)}
        queued = client.post(
            f"/api/sidebar-threads/{sidebar['thread_id']}/queued-messages",
            json={
                "id": "23d58ec5-7d2f-4a41-87e6-21ac50b5921d",
                "content": " redirect ",
                "references": [
                    {"source": "workspace", "path": str(project_file.resolve()), "display_path": "fake.md"},
                    {"source": "workspace", "path": "workspace:README.md", "display_path": "README.md"},
                    {"source": "upload", "path": str(upload_file.resolve()), "display_path": "wrong.txt"},
                ],
            },
        ).json()

        response = client.post(
            f"/api/turns/{turn.id}/steer",
            json={
                "delivery_id": "delivery-1",
                "message_ids": [queued["id"]],
            },
        )

        assert response.status_code == 202
        claimed = state.message_queue.claim(execution_id, "worker")
        assert claimed is not None
        assert claimed.envelope.delivery_id == "delivery-1"
        assert claimed.envelope.message.text == "redirect"
        assert list(tuple(claimed.envelope.message.reference_dicts())) == [
            {"source": "workspace", "path": "workspace:README.md", "display_path": "workspace:README.md"},
            {"source": "upload", "path": "workspace:uploads/notes.txt", "display_path": "workspace:uploads/notes.txt"},
        ]

        state.active_turn_streams.clear()
        assert (
            client.post(
                f"/api/turns/{turn.id}/steer",
                json={
                    "delivery_id": "closed",
                    "message_ids": [queued["id"]],
                },
            ).status_code
            == 409
        )

        failed = turn.clone()
        failed.status = "failed"
        store.update_node(failed)
        state.active_turn_streams[turn.id] = SimpleNamespace(turn_id=turn.id)
        assert (
            client.post(
                f"/api/turns/{turn.id}/steer",
                json={
                    "delivery_id": "failed",
                    "message_ids": [queued["id"]],
                },
            ).status_code
            == 409
        )
