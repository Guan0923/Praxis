from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.domain.runtime_state import NodeWriter, RuntimeNode, RuntimeRootState, RuntimeState
from backend.storage import MemoryMessageQueue, SQLiteSessionStore


def _store(state: WebAppState) -> SQLiteSessionStore:
    return SQLiteSessionStore(state.paths, state.agent_thread_index)


def _turn(
    store: SQLiteSessionStore,
    *,
    session_id: str,
    thread_id: str,
    turn_id: str,
    parent: RuntimeRootState | RuntimeState,
    prompt: str,
    answer: str | None = "answer",
) -> RuntimeState:
    writer = NodeWriter(store)
    node = writer.create(
        session_id=session_id,
        thread_id=thread_id,
        id=turn_id,
        parent=parent,
        user_content=prompt,
    )
    if answer is not None:
        node = writer.append_item(node, {"type": "text", "text": answer, "status": "success"})
        node = writer.finalize(node, "success")
    return node


def _summary(client: TestClient, thread_id: str) -> dict[str, object]:
    return next(
        item
        for item in client.get("/api/sidebar-threads", params={"state": "all"}).json()
        if item["thread_id"] == thread_id
    )


def test_sidebar_summary_tracks_durable_messages_without_opening_the_conversation(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".praxis", message_queue=MemoryMessageQueue())
    with TestClient(create_app(state)) as client:
        created = client.post("/api/sidebar-threads", json={}).json()
        assert created["message_count"] == 0
        assert created["conversation_updated_at"] == created["created_at"]

        store = _store(state)
        root = store.ensure_root_node(created["session_id"], id="summary-root")
        writer = NodeWriter(store)
        running = writer.create(
            session_id=created["session_id"],
            thread_id=created["thread_id"],
            id="summary-running",
            parent=root,
            user_content="question",
        )

        persisted_user = _summary(client, created["thread_id"])
        assert persisted_user["message_count"] == 1
        assert persisted_user["conversation_updated_at"] != created["conversation_updated_at"]

        running = writer.append_item(
            running,
            {"type": "text", "text": "streamed answer", "status": "success"},
        )
        writer.finalize(running, "success")
        completed = _summary(client, created["thread_id"])
        assert completed["message_count"] == 2

        renamed = client.patch(
            f"/api/sidebar-threads/{created['thread_id']}",
            json={"title": "renamed"},
        ).json()
        assert renamed["conversation_updated_at"] == completed["conversation_updated_at"]
        assert renamed["updated_at"] != created["updated_at"]


def test_sidebar_summary_follows_each_thread_head_and_excludes_sibling_branches(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".praxis", message_queue=MemoryMessageQueue())
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={"title": "source"}).json()
        store = _store(state)
        root = store.ensure_root_node(sidebar["session_id"], id="branch-root")
        first = _turn(
            store,
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            turn_id="main-first",
            parent=root,
            prompt="first",
        )
        second = _turn(
            store,
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            turn_id="main-second",
            parent=first,
            prompt="second",
        )

        fork_response = client.post(
            f"/api/turns/{first.id}/fork",
            json={"id": "fork-copy", "thread_id": "thread-fork"},
        )
        assert fork_response.status_code == 201
        fork_payload = fork_response.json()
        assert fork_payload["sidebar_thread"]["message_count"] == 2
        forked = store.get_node(sidebar["session_id"], "fork-copy")
        assert isinstance(forked, RuntimeState)

        _turn(
            store,
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            turn_id="main-third",
            parent=second,
            prompt="main only",
        )
        _turn(
            store,
            session_id=sidebar["session_id"],
            thread_id="thread-fork",
            turn_id="fork-second",
            parent=forked,
            prompt="fork only",
        )

        summaries = client.get("/api/sidebar-threads", params={"state": "all"}).json()
        by_thread = {item["thread_id"]: item for item in summaries}
        assert by_thread[sidebar["thread_id"]]["message_count"] == 6
        assert by_thread["thread-fork"]["message_count"] == 4
        assert [item["conversation_updated_at"] for item in summaries] == sorted(
            (item["conversation_updated_at"] for item in summaries),
            reverse=True,
        )

        rewound = store.append_turn_version(
            second.id,
            {"type": "text", "text": "second rewritten", "status": "success"},
        )
        rewound.data[rewound.current_data_idx][-1]["content"] = [
            {"type": "text", "text": "rewritten answer", "status": "success"}
        ]
        rewound.status = "success"
        store.finalize_node(rewound)

        assert _summary(client, sidebar["thread_id"])["message_count"] == 4
        assert _summary(client, "thread-fork")["message_count"] == 4


def test_sidebar_summary_crud_responses_keep_the_same_contract(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".praxis", message_queue=MemoryMessageQueue())
    with TestClient(create_app(state)) as client:
        created = client.post("/api/sidebar-threads", json={}).json()
        expected = {"message_count", "conversation_updated_at"}
        assert expected <= created.keys()
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=active").json()] == [
            created["thread_id"]
        ]

        archived = client.post(f"/api/sidebar-threads/{created['thread_id']}/archive").json()
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=archived").json()] == [
            created["thread_id"]
        ]
        restored = client.post(f"/api/sidebar-threads/{created['thread_id']}/restore").json()
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=active").json()] == [
            created["thread_id"]
        ]
        deleted = client.delete(f"/api/sidebar-threads/{created['thread_id']}").json()
        assert expected <= archived.keys()
        assert expected <= restored.keys()
        assert expected <= deleted.keys()
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=deleted").json()] == [
            created["thread_id"]
        ]
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=all").json()] == [
            created["thread_id"]
        ]


@pytest.mark.parametrize("has_previous_turn", [False, True])
def test_sidebar_refresh_keeps_one_snapshot_during_turn_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_previous_turn: bool
) -> None:
    state = WebAppState(tmp_path / ".praxis", message_queue=MemoryMessageQueue())
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        session_id, thread_id = sidebar["session_id"], sidebar["thread_id"]
        store = _store(state)
        parent = store.ensure_root_node(session_id, id="snapshot-root")
        if has_previous_turn:
            parent = _turn(
                store, session_id=session_id, thread_id=thread_id, turn_id="previous", parent=parent, prompt="previous"
            )
        previous = _summary(client, thread_id)

        # Only this test database uses WAL so the writer can commit while the reader is paused.
        with sqlite3.connect(state.paths.session_db(session_id)) as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        nodes_read, writer_done = Event(), Event()
        original_objects = SQLiteSessionStore._objects

        def read_objects(connection: sqlite3.Connection, selected_session_id: str, namespace: str) -> list[RuntimeNode]:
            objects = original_objects(connection, selected_session_id, namespace)
            if selected_session_id == session_id and namespace == "runtime_node" and not nodes_read.is_set():
                nodes_read.set()
                if not writer_done.wait(timeout=10):
                    raise TimeoutError("Concurrent Turn creation did not finish")
            return objects

        monkeypatch.setattr(SQLiteSessionStore, "_objects", staticmethod(read_objects))
        with ThreadPoolExecutor(max_workers=1) as executor:
            refresh = executor.submit(client.get, "/api/sidebar-threads", params={"state": "all"})
            try:
                assert nodes_read.wait(timeout=10), "Sidebar did not read the node snapshot"
                _turn(store, session_id=session_id, thread_id=thread_id, turn_id="new", parent=parent, prompt="new")
            finally:
                writer_done.set()
            response = refresh.result(timeout=10)
        assert response.status_code == 200
        current = next(item for item in response.json() if item["thread_id"] == thread_id)
        assert current["message_count"] == previous["message_count"]
        assert current["conversation_updated_at"] == previous["conversation_updated_at"]
        following = _summary(client, thread_id)
        assert following["message_count"] == previous["message_count"] + 2
        assert following["conversation_updated_at"] > previous["conversation_updated_at"]


def test_sidebar_summary_does_not_hide_a_broken_turn_reference(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".praxis", message_queue=MemoryMessageQueue())
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = _store(state)
        store.ensure_root_node(sidebar["session_id"], id="broken-reference-root")
        with sqlite3.connect(state.paths.session_db(sidebar["session_id"])) as connection:
            connection.execute(
                "UPDATE runtime_threads SET current_turn_id=? WHERE thread_id=?",
                ("missing-turn", sidebar["thread_id"]),
            )
        with pytest.raises(KeyError, match="Unknown Turn"):
            store.list_sidebar_thread_summaries()
