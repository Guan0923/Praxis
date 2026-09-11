"""Process-owned, Turn-scoped Todo storage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from threading import RLock
from typing import Any
from uuid import uuid4

from backend.domain.todo import TodoSnapshot, TodoStateError, TodoUpdateResult, apply_todo_operations

_MAX_TRANSACTION_RETRIES = 16


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(expected_revision: int, operations: Sequence[Mapping[str, Any]]) -> str:
    payload = _json({"expected_revision": expected_revision, "operations": list(operations)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MemoryTodoListStore:
    """Thread-safe Turn-scoped Todo state."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], TodoSnapshot] = {}
        self._receipts: dict[tuple[str, str, str], tuple[str, TodoUpdateResult]] = {}
        self._finalization: set[tuple[str, str]] = set()
        self._compaction_sources: dict[tuple[str, str], str] = {}
        self._lock = RLock()

    def update(
        self,
        *,
        session_id: str,
        turn_id: str,
        call_id: str,
        expected_revision: int,
        operations: Sequence[Mapping[str, Any]],
    ) -> TodoUpdateResult:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise TodoStateError("invalid_revision", "Expected revision must be a non-negative integer.")
        fingerprint = _fingerprint(expected_revision, operations)
        receipt_key = (session_id, turn_id, call_id)
        with self._lock:
            receipt = self._receipts.get(receipt_key)
            if receipt is not None:
                if receipt[0] != fingerprint:
                    raise TodoStateError("call_id_conflict", "Call ID was already used with different arguments.")
                return receipt[1]
            current = self._states.get((session_id, turn_id), TodoSnapshot())
            if current.revision != expected_revision:
                raise TodoStateError(
                    "revision_conflict",
                    f"Expected revision {expected_revision}, current revision is {current.revision}.",
                    snapshot=current,
                )
            generated = tuple(f"todo_{uuid4().hex}" for operation in operations if operation.get("op") == "add")
            updated, applied = apply_todo_operations(current, operations, generated_ids=generated)
            result = TodoUpdateResult(turn_id, updated, applied)
            self._states[(session_id, turn_id)] = updated
            self._receipts[receipt_key] = (fingerprint, result)
            return result

    def snapshot(self, session_id: str, turn_id: str) -> TodoSnapshot:
        with self._lock:
            return self._states.get((session_id, turn_id), TodoSnapshot())

    def receipt(self, session_id: str, turn_id: str, call_id: str) -> TodoUpdateResult | None:
        with self._lock:
            receipt = self._receipts.get((session_id, turn_id, call_id))
            return receipt[1] if receipt is not None else None

    def copy_for_compaction(
        self,
        session_id: str,
        source_turn_id: str,
        target_turn_id: str,
        *,
        expected_revision: int,
    ) -> TodoSnapshot | None:
        source_key = (session_id, source_turn_id)
        target_key = (session_id, target_turn_id)
        with self._lock:
            source = self._states.get(source_key)
            if source is None:
                return None
            if source.revision != expected_revision:
                raise TodoStateError(
                    "revision_conflict",
                    f"Expected revision {expected_revision}, current revision is {source.revision}.",
                    snapshot=source,
                )
            existing = self._states.get(target_key)
            if existing is not None:
                if self._compaction_sources.get(target_key) != source_turn_id:
                    raise TodoStateError(
                        "target_conflict",
                        f"Target Turn {target_turn_id!r} already has unrelated Todo state.",
                    )
                return existing
            self._states[target_key] = source
            self._compaction_sources[target_key] = source_turn_id
            if source_key in self._finalization:
                self._finalization.add(target_key)
            return source

    def claim_finalization(self, session_id: str, turn_id: str) -> bool:
        key = (session_id, turn_id)
        with self._lock:
            if not self.snapshot(session_id, turn_id).unfinished or key in self._finalization:
                return False
            self._finalization.add(key)
            return True

    def finalization_claimed(self, session_id: str, turn_id: str) -> bool:
        with self._lock:
            return (session_id, turn_id) in self._finalization

    def release_turns(self, session_id: str, turn_ids: set[str]) -> None:
        with self._lock:
            for values in (self._states, self._receipts, self._compaction_sources):
                for key in list(values):
                    if key[0] == session_id and key[1] in turn_ids:
                        del values[key]
            self._finalization.difference_update(
                {key for key in self._finalization if key[0] == session_id and key[1] in turn_ids}
            )

    def close(self) -> None:
        with self._lock:
            self._states.clear()
            self._receipts.clear()
            self._finalization.clear()
            self._compaction_sources.clear()

    def persist_turn(self, session_id: str, turn_id: str) -> None:
        return None

    def expire_turn(self, session_id: str, turn_id: str) -> None:
        return None


__all__ = ["MemoryTodoListStore"]
