from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.domain import TodoStateError
from backend.storage.todo_list import MemoryTodoListStore


@pytest.fixture
def memory_todo_store():
    store = MemoryTodoListStore()
    yield store
    store.close()


def test_memory_todo_transaction_is_atomic_and_idempotent(
    memory_todo_store: MemoryTodoListStore,
) -> None:
    store = memory_todo_store
    operations = [
        {"op": "add", "content": "same", "status": "in_progress"},
        {"op": "add", "content": "same", "status": "in_progress"},
    ]

    first = store.update(
        session_id="session",
        turn_id="turn",
        call_id="call-add",
        expected_revision=0,
        operations=operations,
    )
    replay = store.update(
        session_id="session",
        turn_id="turn",
        call_id="call-add",
        expected_revision=0,
        operations=operations,
    )
    todo_id = first.snapshot.todos[0].id

    with pytest.raises(TodoStateError, match="more than once"):
        store.update(
            session_id="session",
            turn_id="turn",
            call_id="call-invalid",
            expected_revision=1,
            operations=[
                {"op": "update", "id": todo_id, "status": "completed"},
                {"op": "remove", "id": todo_id},
            ],
        )

    assert replay == first
    assert store.snapshot("session", "turn") == first.snapshot


def test_memory_rejects_stale_revision_and_conflicting_call_id(
    memory_todo_store: MemoryTodoListStore,
) -> None:
    store = memory_todo_store
    operations = [{"op": "add", "content": "work", "status": "pending"}]
    store.update(
        session_id="session",
        turn_id="turn",
        call_id="call",
        expected_revision=0,
        operations=operations,
    )

    with pytest.raises(TodoStateError) as stale:
        store.update(
            session_id="session",
            turn_id="turn",
            call_id="stale",
            expected_revision=0,
            operations=operations,
        )
    with pytest.raises(TodoStateError) as conflict:
        store.update(
            session_id="session",
            turn_id="turn",
            call_id="call",
            expected_revision=1,
            operations=[{"op": "add", "content": "different", "status": "pending"}],
        )

    assert stale.value.code == "revision_conflict"
    assert stale.value.snapshot is not None and stale.value.snapshot.revision == 1
    assert conflict.value.code == "call_id_conflict"
    assert store.snapshot("session", "turn").revision == 1


def test_memory_finalization_without_expiry(
    memory_todo_store: MemoryTodoListStore,
) -> None:
    store = memory_todo_store
    store.update(
        session_id="session",
        turn_id="turn",
        call_id="call",
        expected_revision=0,
        operations=[{"op": "add", "content": "work", "status": "pending"}],
    )

    assert store.claim_finalization("session", "turn") is True
    assert store.claim_finalization("session", "turn") is False
    assert store.finalization_claimed("session", "turn") is True
    store.expire_turn("session", "turn")
    store.persist_turn("session", "turn")


def test_memory_allows_only_one_concurrent_writer(
    memory_todo_store: MemoryTodoListStore,
) -> None:
    store = memory_todo_store

    def update(call_id: str):
        try:
            return store.update(
                session_id="session",
                turn_id="turn",
                call_id=call_id,
                expected_revision=0,
                operations=[{"op": "add", "content": call_id, "status": "pending"}],
            )
        except TodoStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, ["concurrent-a", "concurrent-b"]))

    assert sum(not isinstance(outcome, TodoStateError) for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, TodoStateError)]
    assert len(conflicts) == 1 and conflicts[0].code == "revision_conflict"
    assert store.snapshot("session", "turn").revision == 1


def test_memory_isolates_sessions_and_turns_even_when_call_ids_match(
    memory_todo_store: MemoryTodoListStore,
) -> None:
    store = memory_todo_store
    operation = [{"op": "add", "content": "isolated", "status": "pending"}]

    first = store.update(
        session_id="session-a",
        turn_id="turn-a",
        call_id="same-call",
        expected_revision=0,
        operations=operation,
    )
    second = store.update(
        session_id="session-a",
        turn_id="turn-b",
        call_id="same-call",
        expected_revision=0,
        operations=operation,
    )
    third = store.update(
        session_id="session-b",
        turn_id="turn-a",
        call_id="same-call",
        expected_revision=0,
        operations=operation,
    )

    assert first.snapshot.revision == second.snapshot.revision == third.snapshot.revision == 1
    assert len({first.snapshot.todos[0].id, second.snapshot.todos[0].id, third.snapshot.todos[0].id}) == 3


def test_memory_store_compaction_copy_preserves_snapshot_and_finalization_without_overwriting_target() -> None:
    store = MemoryTodoListStore()
    source = store.update(
        session_id="session",
        turn_id="turn-source",
        call_id="source-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "continue after compact", "status": "in_progress"}],
    )
    assert store.claim_finalization("session", "turn-source") is True

    copied = store.copy_for_compaction(
        "session",
        "turn-source",
        "turn-target",
        expected_revision=source.snapshot.revision,
    )

    assert copied == source.snapshot
    assert store.snapshot("session", "turn-target") == source.snapshot
    assert store.finalization_claimed("session", "turn-target") is True
    target_updated = store.update(
        session_id="session",
        turn_id="turn-target",
        call_id="target-complete",
        expected_revision=1,
        operations=[{"op": "update", "id": source.snapshot.todos[0].id, "status": "completed"}],
    )

    replay = store.copy_for_compaction(
        "session",
        "turn-source",
        "turn-target",
        expected_revision=source.snapshot.revision,
    )

    assert replay == target_updated.snapshot
    assert store.snapshot("session", "turn-target") == target_updated.snapshot
    assert store.snapshot("session", "turn-source") == source.snapshot


def test_memory_store_compaction_copy_distinguishes_absent_and_initialized_empty_todos() -> None:
    store = MemoryTodoListStore()

    assert store.copy_for_compaction("session", "missing", "target", expected_revision=0) is None
    added = store.update(
        session_id="session",
        turn_id="source",
        call_id="add",
        expected_revision=0,
        operations=[{"op": "add", "content": "temporary", "status": "pending"}],
    )
    emptied = store.update(
        session_id="session",
        turn_id="source",
        call_id="remove",
        expected_revision=1,
        operations=[{"op": "remove", "id": added.snapshot.todos[0].id}],
    )

    copied = store.copy_for_compaction(
        "session",
        "source",
        "empty-target",
        expected_revision=emptied.snapshot.revision,
    )

    assert copied is not None and copied.revision == 2 and copied.todos == ()


def test_memory_store_compaction_copy_rejects_an_unrelated_target() -> None:
    store = MemoryTodoListStore()
    source = store.update(
        session_id="session",
        turn_id="source",
        call_id="source-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "source", "status": "pending"}],
    )
    store.update(
        session_id="session",
        turn_id="target",
        call_id="target-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "target", "status": "pending"}],
    )

    with pytest.raises(TodoStateError) as conflict:
        store.copy_for_compaction(
            "session",
            "source",
            "target",
            expected_revision=source.snapshot.revision,
        )

    assert conflict.value.code == "target_conflict"
    assert store.snapshot("session", "target").todos[0].content == "target"


def test_memory_compaction_copy_preserves_state_without_expiry(
    memory_todo_store: MemoryTodoListStore,
) -> None:
    store = memory_todo_store
    source = store.update(
        session_id="session",
        turn_id="turn-source",
        call_id="source-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "continue", "status": "pending"}],
    )
    assert store.claim_finalization("session", "turn-source") is True

    copied = store.copy_for_compaction(
        "session",
        "turn-source",
        "turn-target",
        expected_revision=1,
    )

    assert copied == source.snapshot
    assert store.snapshot("session", "turn-target") == source.snapshot
    assert store.finalization_claimed("session", "turn-target") is True
    store.expire_turn("session", "turn-source")


def test_memory_compaction_copy_handles_completed_empty_absent_and_repeated_targets(
    memory_todo_store: MemoryTodoListStore,
) -> None:
    store = memory_todo_store
    assert store.copy_for_compaction("session", "missing", "missing-target", expected_revision=0) is None

    completed = store.update(
        session_id="session",
        turn_id="completed-source",
        call_id="completed-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "already done", "status": "completed"}],
    )
    copied = store.copy_for_compaction(
        "session",
        "completed-source",
        "completed-target",
        expected_revision=completed.snapshot.revision,
    )
    assert copied == completed.snapshot
    assert [todo.status for todo in store.snapshot("session", "completed-target").todos] == ["completed"]

    target_updated = store.update(
        session_id="session",
        turn_id="completed-target",
        call_id="target-update",
        expected_revision=completed.snapshot.revision,
        operations=[
            {
                "op": "update",
                "id": completed.snapshot.todos[0].id,
                "content": "updated only on target",
            }
        ],
    )
    replayed = store.copy_for_compaction(
        "session",
        "completed-source",
        "completed-target",
        expected_revision=completed.snapshot.revision,
    )
    assert replayed == target_updated.snapshot
    assert store.snapshot("session", "completed-target") == target_updated.snapshot
    assert store.snapshot("session", "completed-source") == completed.snapshot

    temporary = store.update(
        session_id="session",
        turn_id="empty-source",
        call_id="empty-add",
        expected_revision=0,
        operations=[{"op": "add", "content": "remove me", "status": "pending"}],
    )
    emptied = store.update(
        session_id="session",
        turn_id="empty-source",
        call_id="empty-remove",
        expected_revision=temporary.snapshot.revision,
        operations=[{"op": "remove", "id": temporary.snapshot.todos[0].id}],
    )
    empty_copy = store.copy_for_compaction(
        "session",
        "empty-source",
        "empty-target",
        expected_revision=emptied.snapshot.revision,
    )
    assert empty_copy is not None and empty_copy.revision == 2 and empty_copy.todos == ()
