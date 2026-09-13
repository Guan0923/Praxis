"""Process-owned message queues with atomic admission and acknowledgment."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from threading import Condition, Event, RLock

from backend.domain.input_message import InputMessage
from backend.domain.message_queue import (
    ClaimedEnvelope,
    DeliveryConflict,
    MessageEnvelope,
    MessageQueueUnavailable,
    QueuedMessage,
    QueueItemConflict,
    QueueItemNotFound,
    QueueItemStateConflict,
    TurnStart,
    queue_utc_now,
)

from .message_queue_support import _dispatch_identity, _envelope_identity, _merge, _same_create


@dataclass(frozen=True, slots=True)
class AcknowledgedDelivery:
    delivery_id: str
    target_id: str
    session_id: str
    thread_id: str


class MemoryMessageQueue:
    """Thread-safe delivery for one backend process."""

    def __init__(self) -> None:
        self._queues: dict[str, list[QueuedMessage]] = {}
        self._streams: dict[str, list[tuple[str, MessageEnvelope, float, str | None]]] = {}
        self._thread_streams: dict[str, list[tuple[str, MessageEnvelope, float, str | None]]] = {}
        self._report_streams: dict[str, list[tuple[str, MessageEnvelope, float, str | None]]] = {}
        self._turn_start_stream: list[tuple[str, MessageEnvelope, float, str | None]] = []
        self._receipts: dict[str, tuple[object, str, MessageEnvelope | AcknowledgedDelivery]] = {}
        self._counter = 0
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._closed = False
        self._deleted_threads: set[str] = set()
        self.on_change = None
        self.on_queue_change: Callable[[str], None] | None = None

    def _queue_changed(self, thread_id: str) -> None:
        if self.on_queue_change is not None:
            self.on_queue_change(thread_id)

    @property
    def admission_lock(self):
        return self._lock

    def has_pending(self, thread_id: str) -> bool:
        with self._lock:
            if (
                self._queues.get(thread_id)
                or self._thread_streams.get(thread_id)
                or self._report_streams.get(thread_id)
            ):
                return True
            return any(
                envelope.thread_id == thread_id
                for entries in [self._turn_start_stream, *self._streams.values()]
                for _, envelope, _, _ in entries
            )

    def release_thread_cache(self, thread_id: str, turn_ids: Sequence[str] = ()) -> None:
        with self._lock:
            if self.has_pending(thread_id):
                return
            self._queues.pop(thread_id, None)
            self._thread_streams.pop(thread_id, None)
            self._report_streams.pop(thread_id, None)
            for turn_id in turn_ids:
                if not self._streams.get(turn_id):
                    self._streams.pop(turn_id, None)
            # Receipts survive eviction to distinguish conflicting retries.
            for delivery_id, (identity, status, envelope) in self._receipts.items():
                if (
                    status == "acknowledged"
                    and envelope.thread_id == thread_id
                    and isinstance(envelope, MessageEnvelope)
                ):
                    receipt = AcknowledgedDelivery(
                        envelope.delivery_id, envelope.target_id, envelope.session_id, envelope.thread_id
                    )
                    self._receipts[delivery_id] = (identity, status, receipt)

    def require_thread_open(self, thread_id: str) -> None:
        with self._lock:
            if thread_id in self._deleted_threads:
                raise QueueItemStateConflict("Conversation has been deleted.")

    def restore_threads(self, thread_ids: set[str]) -> None:
        with self._condition:
            self._deleted_threads.difference_update(thread_ids)
            self._condition.notify_all()

    def discard_threads(self, thread_ids: set[str]) -> None:
        with self._condition:
            self._deleted_threads.update(thread_ids)
            for thread_id in thread_ids:
                self._queues.pop(thread_id, None)
                self._thread_streams.pop(thread_id, None)
                self._report_streams.pop(thread_id, None)
            self._turn_start_stream[:] = [
                entry for entry in self._turn_start_stream if entry[1].thread_id not in thread_ids
            ]
            for key, entries in list(self._streams.items()):
                self._streams[key] = [entry for entry in entries if entry[1].thread_id not in thread_ids]
            for delivery_id, (_, _, envelope) in list(self._receipts.items()):
                if envelope.thread_id in thread_ids:
                    self._receipts.pop(delivery_id, None)
            self._condition.notify_all()

    def ping(self) -> None:
        with self._lock:
            if self._closed:
                raise MessageQueueUnavailable("Backend message queue is closed.")

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._queues.clear()
            self._streams.clear()
            self._thread_streams.clear()
            self._report_streams.clear()
            self._turn_start_stream.clear()
            self._receipts.clear()
            self._condition.notify_all()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def wait_turn_start(self, consumer: str, stop: Event) -> ClaimedEnvelope | None:
        with self._condition:
            self._condition.wait_for(
                lambda: (
                    self._closed or stop.is_set() or any(owner is None for _, _, _, owner in self._turn_start_stream)
                )
            )
            if self._closed or stop.is_set():
                return None
            return self.claim_turn_start(consumer)

    def retry(self, claimed: ClaimedEnvelope) -> None:
        with self._condition:
            envelope = claimed.envelope
            entries = (
                self._turn_start_stream
                if envelope.target_kind == "turn_start"
                else self._thread_streams.get(envelope.target_id, [])
                if envelope.target_kind == "thread"
                else self._report_streams.get(envelope.target_id, [])
                if envelope.target_kind == "report"
                else self._streams.get(envelope.target_id, [])
            )
            for i, (stream_id, stored, count, _) in enumerate(entries):
                if stream_id == claimed.stream_id:
                    entries[i] = (stream_id, stored, count, None)
                    self._condition.notify_all()
                    break

    def list(self, thread_id: str) -> list[QueuedMessage]:
        with self._lock:
            return list(self._queues.get(thread_id, ()))

    def create(self, item: QueuedMessage) -> tuple[QueuedMessage, bool]:
        with self._lock:
            self.ping()
            self.require_thread_open(item.thread_id)
            items = self._queues.setdefault(item.thread_id, [])
            existing = next((value for value in items if value.id == item.id), None)
            if existing is not None:
                if _same_create(existing, item):
                    return existing, False
                raise QueueItemConflict("queued_message_id_conflict")
            items.append(item)
            self._queue_changed(item.thread_id)
            return item, True

    def update(self, thread_id: str, message_id: str, *, message: InputMessage) -> QueuedMessage:
        with self._lock:
            items = self._queues.get(thread_id, [])
            for index, item in enumerate(items):
                if item.id != message_id:
                    continue
                if item.state != "pending":
                    raise QueueItemStateConflict("queued_message_dispatched")
                items[index] = replace(item, message=message, updated_at=queue_utc_now())
                self._queue_changed(thread_id)
                return items[index]
        raise QueueItemNotFound("queued_message_not_found")

    def delete(self, thread_id: str, message_id: str) -> None:
        with self._lock:
            items = self._queues.get(thread_id, [])
            for index, item in enumerate(items):
                if item.id == message_id:
                    if item.state != "pending":
                        raise QueueItemStateConflict("queued_message_dispatched")
                    items.pop(index)
                    self._queue_changed(thread_id)
                    if self.on_change is not None:
                        self.on_change()
                    return
        raise QueueItemNotFound("queued_message_not_found")

    def pending_message(self, thread_id: str, message_ids: Sequence[str]) -> InputMessage:
        with self._lock:
            self.ping()
            self.require_thread_open(thread_id)
            requested = set(message_ids)
            if len(requested) != len(message_ids):
                raise QueueItemConflict("duplicate_queued_message_id")
            selected = [item for item in self._queues.get(thread_id, []) if item.id in requested]
            if len(selected) != len(message_ids):
                raise QueueItemNotFound("queued_message_not_found")
            if any(item.state != "pending" for item in selected):
                raise QueueItemStateConflict("queued_message_dispatched")
            return _merge(selected)

    def dispatch(
        self,
        *,
        delivery_id: str,
        message_ids: Sequence[str],
        session_id: str,
        thread_id: str,
        turn_id: str,
        correlation_id: str | None = None,
        start: TurnStart | None = None,
    ) -> MessageEnvelope | AcknowledgedDelivery:
        with self._lock:
            self.ping()
            self.require_thread_open(thread_id)
            identity = _dispatch_identity(thread_id, turn_id, message_ids)
            receipt = self._receipts.get(delivery_id)
            if receipt is not None:
                if receipt[0] != identity:
                    raise DeliveryConflict("delivery_id_conflict")
                if receipt[1] != "returned":
                    return receipt[2]
            by_id = {item.id: item for item in self._queues.get(thread_id, [])}
            try:
                selected = [by_id[item.id] for item in self._queues.get(thread_id, []) if item.id in set(message_ids)]
            except KeyError as exc:
                raise QueueItemNotFound("queued_message_not_found") from exc
            if len(selected) != len(message_ids):
                raise QueueItemNotFound("queued_message_not_found")
            if any(item.state != "pending" for item in selected):
                raise QueueItemStateConflict("queued_message_dispatched")
            message = _merge(selected)
            envelope = (
                receipt[2]
                if receipt is not None
                else MessageEnvelope(
                    delivery_id,
                    "user",
                    thread_id,
                    "turn_start" if start is not None else "turn",
                    turn_id,
                    session_id,
                    thread_id,
                    message,
                    tuple(item.id for item in selected),
                    correlation_id=correlation_id,
                    start=replace(start, queued=True) if start is not None else None,
                )
            )
            selected_ids = set(envelope.source_message_ids)
            self._queues[thread_id] = [
                replace(item, state="dispatched", updated_at=queue_utc_now()) if item.id in selected_ids else item
                for item in self._queues[thread_id]
            ]
            self._counter += 1
            entry = (f"{self._counter}-0", envelope, 0.0, None)
            if start is not None:
                self._turn_start_stream.append(entry)
            else:
                self._streams.setdefault(turn_id, []).append(entry)
            self._receipts[delivery_id] = (identity, "dispatched", envelope)
            self._queue_changed(thread_id)
            self._condition.notify_all()
            return envelope

    def dispatch_turn_start(self, envelope: MessageEnvelope) -> MessageEnvelope | AcknowledgedDelivery:
        if envelope.sender_kind != "user" or envelope.target_kind != "turn_start":
            raise ValueError("Turn-start dispatch requires sender_kind=user and target_kind=turn_start.")
        with self._lock:
            self.ping()
            self.require_thread_open(envelope.thread_id)
            identity = _envelope_identity(envelope)
            receipt = self._receipts.get(envelope.delivery_id)
            if receipt is not None:
                if receipt[0] != identity:
                    raise DeliveryConflict("delivery_id_conflict")
                return receipt[2]
            canonical = replace(envelope, attempts=0)
            self._counter += 1
            self._turn_start_stream.append((f"{self._counter}-0", canonical, 0.0, None))
            self._receipts[envelope.delivery_id] = (identity, "dispatched", canonical)
            self._condition.notify_all()
            return canonical

    def dispatch_agent(self, envelope: MessageEnvelope) -> MessageEnvelope | AcknowledgedDelivery:
        if envelope.sender_kind != "agent" or envelope.target_kind != "thread":
            raise ValueError("Agent dispatch requires sender_kind=agent and target_kind=thread.")
        with self._lock:
            self.ping()
            self.require_thread_open(envelope.thread_id)
            identity = _envelope_identity(envelope)
            receipt = self._receipts.get(envelope.delivery_id)
            if receipt is not None:
                if receipt[0] != identity:
                    raise DeliveryConflict("delivery_id_conflict")
                return receipt[2]
            canonical = replace(envelope, attempts=0)
            self._counter += 1
            self._thread_streams.setdefault(envelope.target_id, []).append((f"{self._counter}-0", canonical, 0.0, None))
            self._receipts[envelope.delivery_id] = (identity, "dispatched", canonical)
            self._condition.notify_all()
            return canonical

    @contextmanager
    def prepare_reports(self) -> Iterator[Callable[[MessageEnvelope], None]]:
        """Hold admission through SQLite commit; staged entries cannot be claimed."""
        with self._condition:
            self.ping()
            staged: list[tuple[str, MessageEnvelope, float, str | None]] = []

            def stage(envelope: MessageEnvelope) -> None:
                if envelope.sender_kind != "agent" or envelope.target_kind != "report":
                    raise ValueError("Report dispatch requires an Agent report.")
                self.require_thread_open(envelope.thread_id)
                self._counter += 1
                entry = (f"{self._counter}-0", envelope, 0.0, "preparing")
                self._report_streams.setdefault(envelope.target_id, []).append(entry)
                staged.append(entry)

            try:
                yield stage
            except BaseException:
                for entry in staged:
                    entries = self._report_streams.get(entry[1].target_id, [])
                    entries[:] = [value for value in entries if value is not entry]
                raise
            else:
                for entry in staged:
                    entries = self._report_streams[entry[1].target_id]
                    index = next(i for i, value in enumerate(entries) if value is entry)
                    entries[index] = (entry[0], entry[1], 0.0, None)
                self._condition.notify_all()

    def has_start_request(self, thread_id: str) -> bool:
        with self._lock:
            return any(entry[1].thread_id == thread_id for entry in self._turn_start_stream)

    def has_reports(self, thread_id: str) -> bool:
        with self._lock:
            entries = self._report_streams.get(thread_id, [])
            return bool(entries and entries[0][3] is None)

    def claim(self, turn_id: str, consumer: str) -> ClaimedEnvelope | None:
        with self._lock:
            entries = self._streams.get(turn_id, [])
            for index, (stream_id, envelope, claimed_at, owner) in enumerate(entries):
                if owner is None:
                    claimed = replace(envelope, attempts=envelope.attempts + 1)
                    entries[index] = (stream_id, claimed, claimed_at + 1, consumer)
                    return ClaimedEnvelope(stream_id, claimed)
            return None

    def claim_turn_start(self, consumer: str) -> ClaimedEnvelope | None:
        with self._lock:
            for index, (stream_id, envelope, claimed_at, owner) in enumerate(self._turn_start_stream):
                if owner is None:
                    claimed = replace(envelope, attempts=envelope.attempts + 1)
                    self._turn_start_stream[index] = (stream_id, claimed, claimed_at + 1, consumer)
                    return ClaimedEnvelope(stream_id, claimed)
            return None

    def claim_thread(self, thread_id: str, consumer: str) -> ClaimedEnvelope | None:
        with self._lock:
            entries = self._thread_streams.get(thread_id, [])
            for index, (stream_id, envelope, claimed_at, owner) in enumerate(entries):
                if owner is not None:
                    return None
                if owner is None:
                    claimed = replace(envelope, attempts=envelope.attempts + 1)
                    entries[index] = (stream_id, claimed, claimed_at + 1, consumer)
                    return ClaimedEnvelope(stream_id, claimed)
            return None

    def claim_report(self, thread_id: str, consumer: str) -> ClaimedEnvelope | None:
        with self._lock:
            entries = self._report_streams.get(thread_id, [])
            for index, (stream_id, envelope, claimed_at, owner) in enumerate(entries):
                if owner is not None:
                    return None
                if owner is None:
                    claimed = replace(envelope, attempts=envelope.attempts + 1)
                    entries[index] = (stream_id, claimed, claimed_at + 1, consumer)
                    return ClaimedEnvelope(stream_id, claimed)
            return None

    def peek_thread(self, thread_id: str) -> MessageEnvelope | None:
        with self._lock:
            entries = self._thread_streams.get(thread_id, [])
            return entries[0][1] if entries else None

    def ack(self, claimed: ClaimedEnvelope) -> None:
        with self._lock:
            envelope = claimed.envelope
            if self._closed or envelope.thread_id in self._deleted_threads:
                return
            if envelope.target_kind in {"thread", "report"}:
                streams = self._thread_streams if envelope.target_kind == "thread" else self._report_streams
                streams[envelope.target_id] = [
                    item for item in streams.get(envelope.target_id, []) if item[0] != claimed.stream_id
                ]
                if envelope.target_kind != "report":
                    identity, _, stored = self._receipts[envelope.delivery_id]
                    self._receipts[envelope.delivery_id] = (identity, "acknowledged", stored)
                if self.on_change is not None:
                    self.on_change()
                return
            if envelope.target_kind == "turn_start":
                self._turn_start_stream = [item for item in self._turn_start_stream if item[0] != claimed.stream_id]
                if envelope.start is not None and envelope.start.queued:
                    ids = set(envelope.source_message_ids)
                    self._queues[envelope.thread_id] = [
                        item for item in self._queues.get(envelope.thread_id, []) if item.id not in ids
                    ]
                identity, _, stored = self._receipts[envelope.delivery_id]
                self._receipts[envelope.delivery_id] = (identity, "acknowledged", stored)
                self._queue_changed(envelope.thread_id)
                if self.on_change is not None:
                    self.on_change()
                return
            self._streams[envelope.target_id] = [
                item for item in self._streams.get(envelope.target_id, []) if item[0] != claimed.stream_id
            ]
            ids = set(envelope.source_message_ids)
            self._queues[envelope.thread_id] = [
                item for item in self._queues.get(envelope.thread_id, []) if item.id not in ids
            ]
            identity, _, stored = self._receipts[envelope.delivery_id]
            self._receipts[envelope.delivery_id] = (identity, "acknowledged", stored)
            self._queue_changed(envelope.thread_id)
            if self.on_change is not None:
                self.on_change()

    def release_turn(self, turn_id: str) -> None:
        with self._lock:
            entries = self._streams.pop(turn_id, [])
            for _, envelope, _, _ in entries:
                ids = set(envelope.source_message_ids)
                self._queues[envelope.thread_id] = [
                    replace(item, state="pending", updated_at=queue_utc_now()) if item.id in ids else item
                    for item in self._queues.get(envelope.thread_id, [])
                ]
                identity, _, stored = self._receipts[envelope.delivery_id]
                self._receipts[envelope.delivery_id] = (identity, "returned", stored)
                self._queue_changed(envelope.thread_id)


__all__ = ["MemoryMessageQueue"]
