"""Keep running conversations and the five most recently visited idle ones."""

from collections import OrderedDict
from dataclasses import dataclass, field


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

    def touch(self, session_id: str, thread_id: str, turns=()) -> None:
        owner = self.owner(session_id, thread_id)
        with self.lock:
            entry = self.entries.setdefault(owner, ConversationCacheEntry(session_id))
            entry.threads.add(thread_id)
            entry.turns.update(turns)
            self.entries.move_to_end(owner)
            self.trim()

    def begin(self, session_id: str, thread_id: str, turn_id: str) -> None:
        owner = self.owner(session_id, thread_id)
        with self.lock:
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

    def trim(self) -> None:
        with self.lock:
            idle = [
                key
                for key, entry in self.entries.items()
                if not entry.running
                and not any(self.state.message_queue.has_pending(thread) for thread in entry.threads)
            ]
            for key in idle[:-5]:
                entry = self.entries.pop(key)
                # Shared inherited Turns remain available to another cached branch.
                retained = set().union(*(value.turns for value in self.entries.values())) if self.entries else set()
                turns = entry.turns - retained
                for thread in entry.threads:
                    self.state.runtime_event_stream.release_thread(thread, turns)
                    self.state.agent_thread_events.release_thread(entry.session_id, thread)
                self.state.todo_store.release_turns(entry.session_id, turns)
                for thread in entry.threads:
                    self.state.terminal_manager.close_thread(thread)
                for thread in entry.threads:
                    self.state.message_queue.release_thread_cache(thread, tuple(turns))
                for thread in entry.threads:
                    self._owners.pop(thread, None)
