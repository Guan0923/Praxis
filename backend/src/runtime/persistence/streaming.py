"""Bounded, ordered persistence independent of live frame delivery."""

from __future__ import annotations

from queue import Empty, Full, Queue
from threading import Event, Thread

from backend.domain import TracePersistenceError
from backend.domain.runtime_state import NodeFrame


class RuntimeFramePersistence:
    def __init__(self, store, *, capacity: int = 256) -> None:
        if capacity < 1:
            raise ValueError("Runtime persistence requires a positive queue capacity.")
        self.store = store
        self._queue: Queue[tuple[NodeFrame, str, str] | Event | None] = Queue(maxsize=capacity)
        self._error: BaseException | None = None
        self._closed = False
        self._thread = Thread(target=self._run, name="runtime-item-persistence", daemon=True)
        self._thread.start()

    def check(self) -> None:
        if self._error is not None:
            raise TracePersistenceError("Local Item persistence failed; the Turn was stopped.") from self._error

    def _put(self, value) -> None:
        if self._closed:
            raise RuntimeError("Runtime persistence is closed.")
        while True:
            self.check()
            try:
                self._queue.put(value, timeout=0.05)
                return
            except Full:
                continue

    def submit(self, frame: NodeFrame, thread_id: str, status: str) -> None:
        self._put((frame, thread_id, status))

    def flush(self) -> None:
        barrier = Event()
        self._put(barrier)
        while not barrier.wait(0.05):
            self.check()
        self.check()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True
            # The worker drains failed work as well, so shutdown cannot deadlock on a full queue.
            self._queue.put(None)
            self._thread.join()

    def _run(self) -> None:
        try:
            with self.store.streaming_connections():
                while True:
                    work = self._queue.get()
                    try:
                        if work is None:
                            return
                        if isinstance(work, Event):
                            work.set()
                        elif self._error is None:
                            frame, thread_id, status = work
                            self.store.append_runtime_delta(frame, thread_id=thread_id, status=status)
                    except BaseException as exc:
                        self._error = exc
                    finally:
                        self._queue.task_done()
        except BaseException as exc:
            self._error = exc
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except Empty:
                    return
