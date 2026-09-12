"""Keep running conversations and the five most recently visited idle ones."""

import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from backend.domain import QueueItemStateConflict

logger = logging.getLogger(__name__)


@dataclass
class ConversationCacheEntry:
    session_id: str
    turns: set[str] = field(default_factory=set)
    running: set[str] = field(default_factory=set)
    threads: set[str] = field(default_factory=set)


class ConversationCache:
    def __init__(self, state) -> None:
        self.state = state
        self.lock = state.message_queue.admission_lock
        self.entries: OrderedDict[str, ConversationCacheEntry] = OrderedDict()
        self._owners: dict[str, str] = {}
        self._cleanup = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cache-cleanup")

    def owner(self, session_id: str, thread_id: str) -> str:
        with self.lock:
            known = self._owners.get(thread_id)
        if known is not None:
            return known
        store = getattr(self.state, "session_store", None)
        node = store.get_thread_node(session_id, thread_id) if store is not None else None
        owner = node.root_thread_id if node is not None and node.depth > 0 else thread_id
        with self.lock:
            self._owners[thread_id] = owner
        return owner

    def _require_open(self, session_id: str, owner: str) -> None:
        self.state.message_queue.require_thread_open(owner)
        item = self.state.session_store.get_sidebar_thread(owner, session_id=session_id)
        if item is not None and item.deleted_at is not None:
            raise QueueItemStateConflict("Conversation has been deleted.")

    def touch(self, session_id: str, thread_id: str, turns=()) -> None:
        owner = self.owner(session_id, thread_id)
        with self.lock:
            self._require_open(session_id, owner)
            entry = self.entries.setdefault(owner, ConversationCacheEntry(session_id))
            entry.threads.add(thread_id)
            entry.turns.update(turns)
            self.entries.move_to_end(owner)
            self.trim()

    def begin(self, session_id: str, thread_id: str, turn_id: str) -> None:
        owner = self.owner(session_id, thread_id)
        with self.lock:
            self._require_open(session_id, owner)
            entry = self.entries.setdefault(owner, ConversationCacheEntry(session_id))
            entry.threads.add(thread_id)
            entry.turns.add(turn_id)
            entry.running.add(turn_id)

    def finish(self, thread_id: str, turn_id: str) -> None:
        with self.lock:
            owner = next((key for key, item in self.entries.items() if thread_id in item.threads), thread_id)
            entry = self.entries.get(owner)
            if entry is None:
                return
            was_running = turn_id in entry.running
            entry.running.discard(turn_id)
            if was_running:
                self.entries.move_to_end(owner)
            self.trim()

    def contains(self, thread_id: str) -> bool:
        with self.lock:
            return self._owners.get(thread_id, thread_id) in self.entries

    def release(self, thread_id: str) -> None:
        """Let the deletion worker observe cleanup completion and failures."""
        with self.lock:
            entry = self.entries.pop(thread_id, None)
            if entry is None:
                terminal_ids = self.state.terminal_manager.capture_terminal_ids({thread_id})
            else:
                terminal_ids = self._release_entry(entry)
            cleanup = self._cleanup.submit(self._close_terminals, terminal_ids)
        cleanup.result()

    def trim(self) -> None:
        with self.lock:
            terminal_ids: set[str] = set()
            idle = [
                key
                for key, entry in self.entries.items()
                if not entry.running
                and not any(self.state.message_queue.has_pending(thread) for thread in entry.threads)
            ]
            for key in idle[:-5]:
                entry = self.entries.pop(key)
                terminal_ids.update(self._release_entry(entry))
            if terminal_ids:
                self._cleanup.submit(self._close_terminals, terminal_ids)

    def _release_entry(self, entry: ConversationCacheEntry) -> set[str]:
        # Shared inherited Turns remain available to another cached branch.
        retained = set().union(*(value.turns for value in self.entries.values())) if self.entries else set()
        turns = entry.turns - retained
        terminal_ids = self.state.terminal_manager.capture_terminal_ids(entry.threads)
        for thread in entry.threads:
            self.state.runtime_event_stream.release_thread(thread, turns)
            self.state.agent_thread_events.release_thread(entry.session_id, thread)
            self.state.message_queue.release_thread_cache(thread, tuple(turns))
            self._owners.pop(thread, None)
        self.state.todo_store.release_turns(entry.session_id, turns)
        return terminal_ids

    def _close_terminals(self, terminal_ids: set[str]) -> None:
        errors: list[Exception] = []
        for terminal_id in terminal_ids:
            try:
                self.state.terminal_manager.close(terminal_id)
            except Exception as exc:
                logger.exception("Conversation terminal cleanup failed.")
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Conversation terminal cleanup failed.", errors)

    def flush(self) -> None:
        """Wait for previously submitted cleanup without retaining its futures."""
        self._cleanup.submit(lambda: None).result()

    def close(self) -> None:
        self._cleanup.shutdown(wait=True)
