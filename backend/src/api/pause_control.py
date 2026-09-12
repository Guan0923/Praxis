"""Thread-safe pause signalling for one active Turn."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, RLock


class TurnPauseController:
    """Linearize a user pause and fan it out to active operation aborters."""

    def __init__(self) -> None:
        self._requested = Event()
        self._steering = Event()
        self._lock = RLock()
        self._aborters: set[Callable[[], None]] = set()

    def is_requested(self) -> bool:
        return self._requested.is_set()

    def request_pause(self) -> bool:
        return self._interrupt(self._requested)

    def request_steering(self) -> bool:
        return self._interrupt(self._steering)

    def clear_steering(self) -> None:
        self._steering.clear()

    def dispatch_steering(self, dispatch: Callable[[], object]) -> None:
        with self._lock:
            if self.is_requested():
                raise ValueError("Turn is being paused.")
            dispatch()
            aborters = self._accept_interrupt(self._steering)
        self._abort(aborters or ())

    def take_steering(self, take: Callable[[], list]) -> list:
        with self._lock:
            if self.is_requested():
                return []
            self.clear_steering()
            return take()

    def operation_interrupted(self) -> bool:
        return self._requested.is_set() or self._steering.is_set()

    def _interrupt(self, signal: Event) -> bool:
        with self._lock:
            aborters = self._accept_interrupt(signal)
        if aborters is None:
            return False
        self._abort(aborters)
        return True

    def _accept_interrupt(self, signal: Event) -> tuple[Callable[[], None], ...] | None:
        if self._requested.is_set() or signal.is_set():
            return None
        signal.set()
        aborters = tuple(self._aborters)
        # Each registration owns one abort. A concurrent pause must not wait
        # for, or invoke again, an abort already claimed by steering.
        self._aborters.clear()
        return aborters

    @staticmethod
    def _abort(aborters: tuple[Callable[[], None], ...]) -> None:
        errors: list[Exception] = []
        for abort in aborters:
            try:
                abort()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Turn operation interruption failed.", errors)

    def register_abort(self, abort: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self.operation_interrupted():
                abort_now = True
            else:
                self._aborters.add(abort)
                abort_now = False
        if abort_now:
            abort()

        def unregister() -> None:
            with self._lock:
                self._aborters.discard(abort)

        return unregister


__all__ = ["TurnPauseController"]
