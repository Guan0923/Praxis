"""Delete sidebar metadata promptly and finish owned work independently of HTTP."""

from concurrent.futures import ThreadPoolExecutor
from threading import RLock

from backend.domain import error_report


class ConversationDeletion:
    def __init__(self, state) -> None:
        self.state = state
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="conversation-cleanup")
        self._lock = RLock()
        self._results: dict[str, dict] = {}

    def schedule(self, session_id: str, thread_id: str, threads: set[str]) -> None:
        with self._lock:
            previous = self._results.get(thread_id)
            if previous and previous["status"] in {"pending", "completed"}:
                return
            self._results[thread_id] = {"status": "pending"}
            self._executor.submit(self._clean, session_id, thread_id, threads)

    def _clean(self, session_id: str, thread_id: str, threads: set[str]) -> None:
        try:
            self.state.job_registry.close_threads(session_id, threads)
            self.state.message_queue.discard_threads(threads)
            self.state.conversation_cache.release(thread_id)
            for thread in threads:
                self.state.terminal_manager.close_thread(thread)
        except Exception as exc:
            result = {"status": "failed", "error_report": error_report(exc)}
        else:
            result = {"status": "completed"}
        with self._lock:
            self._results[thread_id] = result

    def forget(self, thread_id: str) -> None:
        with self._lock:
            self._results.pop(thread_id, None)

    def result(self, thread_id: str) -> dict:
        with self._lock:
            return dict(self._results.get(thread_id, {"status": "unavailable"}))

    def close(self) -> None:
        self._executor.shutdown(wait=True)
