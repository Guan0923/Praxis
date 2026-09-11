"""Process-owned, bounded fan-out streams for browser observation."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from threading import Condition, RLock

EVENT_STREAM_MAXLEN = 10_000


class RuntimeEventCursorExpired(LookupError):
    """A reader must reload a snapshot after its replay window was evicted."""


@dataclass(frozen=True, slots=True)
class RuntimeStreamEntry:
    stream_id: str
    event_id: str
    sequence: int
    payload: dict[str, object]


class MemoryRuntimeEventStream:
    """Thread-safe fan-out with blocking readers and bounded replay."""

    def __init__(self) -> None:
        self._turns: dict[str, deque[RuntimeStreamEntry]] = {}
        self._threads: dict[str, deque[RuntimeStreamEntry]] = {}
        self._events: dict[str, RuntimeStreamEntry] = {}
        self._event_uses: dict[str, int] = {}
        self._thread_floors: dict[str, int] = {}
        self._condition = Condition(RLock())
        self._closed = False
        self._counter = 0
        self._lock = self._condition

    def publish(
        self,
        *,
        event_id: str,
        turn_id: str,
        thread_id: str,
        sequence: int,
        payload: dict[str, object],
    ) -> tuple[str, str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime event stream is closed.")
            if event_id in self._events:
                existing = self._events[event_id]
                return existing.stream_id, existing.stream_id
            self._counter += 1
            stream_id = str(self._counter)
            entry = RuntimeStreamEntry(stream_id, event_id, sequence, deepcopy(payload))
            self._events[event_id] = entry
            self._append(self._turns.setdefault(turn_id, deque(maxlen=EVENT_STREAM_MAXLEN)), entry)
            thread_entries = self._threads.setdefault(thread_id, deque(maxlen=EVENT_STREAM_MAXLEN))
            if len(thread_entries) == thread_entries.maxlen:
                self._thread_floors[thread_id] = int(thread_entries[0].stream_id)
            self._append(thread_entries, entry)
            self._condition.notify_all()
            return stream_id, stream_id

    def _append(self, entries: deque[RuntimeStreamEntry], entry: RuntimeStreamEntry) -> None:
        if len(entries) == entries.maxlen:
            removed = entries[0].event_id
            self._event_uses[removed] -= 1
            if self._event_uses[removed] == 0:
                del self._event_uses[removed]
                del self._events[removed]
        entries.append(entry)
        self._event_uses[entry.event_id] = self._event_uses.get(entry.event_id, 0) + 1

    def latest_turn_id(self, turn_id: str) -> str:
        with self._lock:
            entries = self._turns.get(turn_id, [])
            return entries[-1].stream_id if entries else "0"

    def latest_turn_event(self, turn_id: str) -> RuntimeStreamEntry | None:
        """Return the newest retained event for one Turn without blocking."""

        with self._lock:
            entries = self._turns.get(turn_id, [])
            return deepcopy(entries[-1]) if entries else None

    def has_event(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._events

    def latest_thread_id(self, thread_id: str) -> str:
        with self._lock:
            entries = self._threads.get(thread_id, [])
            return entries[-1].stream_id if entries else "0"

    @staticmethod
    def _after(
        entries: deque[RuntimeStreamEntry] | list[RuntimeStreamEntry], after_id: str
    ) -> list[RuntimeStreamEntry]:
        if after_id == "0":
            return list(entries)
        return [entry for entry in entries if int(entry.stream_id) > int(after_id)]

    def read_turn(self, turn_id: str, after_id: str, *, block_ms: int = 1000) -> list[RuntimeStreamEntry]:
        return self._read(self._turns, turn_id, after_id, block_ms)

    def read_thread(self, thread_id: str, after_id: str, *, block_ms: int = 1000) -> list[RuntimeStreamEntry]:
        return self._read(self._threads, thread_id, after_id, block_ms)

    def _read(self, streams, key: str, after_id: str, block_ms: int) -> list[RuntimeStreamEntry]:
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed or bool(self._after(streams.get(key, []), after_id)),
                timeout=max(0, block_ms) / 1000,
            )
            if streams is self._threads and int(after_id) < self._thread_floors.get(key, 0):
                raise RuntimeEventCursorExpired("Runtime event replay window was evicted.")
            return deepcopy(self._after(streams.get(key, []), after_id)[:100])

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def release_thread(self, thread_id: str, turn_ids: set[str]) -> None:
        with self._condition:
            groups = [self._threads.pop(thread_id, ())]
            groups.extend(self._turns.pop(turn_id, ()) for turn_id in turn_ids)
            for entries in groups:
                for entry in entries:
                    uses = self._event_uses.get(entry.event_id, 0) - 1
                    if uses <= 0:
                        self._event_uses.pop(entry.event_id, None)
                        self._events.pop(entry.event_id, None)
                    else:
                        self._event_uses[entry.event_id] = uses
            self._thread_floors.pop(thread_id, None)
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._turns.clear()
            self._threads.clear()
            self._events.clear()
            self._event_uses.clear()
            self._thread_floors.clear()
            self._condition.notify_all()
