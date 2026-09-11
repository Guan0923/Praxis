"""Broker-owned actual usage, FIFO admission and newest-first termination."""

import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, RLock, Thread
from time import monotonic

from backend.domain import error_report


def _system_memory_limit_mib() -> int:
    """Return the 80% machine-memory ceiling, with a conservative fallback."""

    total_bytes: int | None = None
    if os.name == "nt":
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_uint32),
                    ("memory_load", ctypes.c_uint32),
                    ("total_physical", ctypes.c_uint64),
                    ("available_physical", ctypes.c_uint64),
                    ("total_page_file", ctypes.c_uint64),
                    ("available_page_file", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64),
                    ("available_virtual", ctypes.c_uint64),
                    ("available_extended", ctypes.c_uint64),
                ]

            status = _MemoryStatus()
            status.length = ctypes.sizeof(_MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total_bytes = int(status.total_physical)
        except (AttributeError, OSError):
            total_bytes = None
    else:
        try:
            total_bytes = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        except (AttributeError, OSError, ValueError):
            total_bytes = None
    if total_bytes is None or total_bytes <= 0:
        return 1_000_000
    return max(128, int(total_bytes * 0.80 / (1024 * 1024)))


def default_aggregate_limits() -> dict[str, int]:
    return {"memory_mib": min(8192, _system_memory_limit_mib()), "processes": 512, "handles": 32768}


def aggregate_limits(values: dict) -> dict[str, int]:
    if set(values) != {"memory_mib", "processes", "handles"}:
        raise ValueError("Invalid aggregate resource fields.")
    for value in values.values():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("Aggregate resource limits must be positive integers.")
    return dict(values)


@dataclass
class Entry:
    owner: str
    sample: Callable
    terminate: Callable
    usage: dict = field(default_factory=dict)
    stopping: bool = False


class AggregateResources:
    def __init__(self, *, clock=monotonic, start: bool = True) -> None:
        self.lock = RLock()
        self.clock = clock
        self.jobs: OrderedDict[str, Entry] = OrderedDict()
        self.queues: dict[str, OrderedDict[str, float]] = {}
        self.permits: dict[str, str] = {}
        self.limits: dict[str, dict] = {}
        self.errors: dict[str, dict] = {}
        self._stop = Event()
        self._thread = Thread(target=self._run, name="sandbox-total-resources", daemon=True)
        if start:
            self._thread.start()

    def configure(self, owner: str, values: dict, *, initialize: bool = False) -> None:
        with self.lock:
            if not initialize or owner not in self.limits:
                self.limits[owner] = aggregate_limits(values)

    def _usage(self, owner: str) -> dict:
        return {
            key: sum(int(entry.usage.get(key, 0)) for entry in self.jobs.values() if entry.owner == owner)
            for key in ("memory_bytes", "processes", "handles")
        }

    def _bounds(self, owner: str) -> dict:
        limits = self.limits.get(owner, default_aggregate_limits())
        return {
            "memory_bytes": limits["memory_mib"] * 1024 * 1024,
            "processes": limits["processes"],
            "handles": limits["handles"],
        }

    def status(self, owner: str) -> dict:
        with self.lock:
            return {
                "usage": self._usage(owner),
                "limits": self.limits.get(owner, default_aggregate_limits()),
                "queued": len(self.queues.get(owner, {})),
                "error_report": self.errors.get(owner),
            }

    def acquire(self, owner: str, ticket: str) -> dict:
        with self.lock:
            queue = self.queues.setdefault(owner, OrderedDict())
            queue[ticket] = self.clock()
            usage, bounds = self._usage(owner), self._bounds(owner)
            available = (
                owner not in self.errors
                and all(usage[key] < bounds[key] for key in bounds)
                and not any(entry.stopping for entry in self.jobs.values() if entry.owner == owner)
            )
            if available and next(iter(queue)) == ticket and self.permits.get(owner) in (None, ticket):
                self.permits[owner] = ticket
            return {**self.status(owner), "granted": self.permits.get(owner) == ticket}

    def cancel(self, owner: str, ticket: str) -> None:
        with self.lock:
            self.queues.get(owner, {}).pop(ticket, None)
            if self.permits.get(owner) == ticket:
                self.permits.pop(owner, None)

    def require_permit(self, owner: str, ticket: str) -> None:
        with self.lock:
            if self.permits.get(owner) != ticket:
                raise ValueError("Model command resource permit is missing or expired.")

    def register(self, owner: str, ticket: str, job_id: str, sample, terminate) -> None:
        with self.lock:
            self.require_permit(owner, ticket)
            entry = Entry(owner, sample, terminate)
            self.jobs[job_id] = entry
            try:
                entry.usage = sample()
            except Exception as exc:
                self.errors[owner] = error_report(exc)
                raise
            finally:
                self.cancel(owner, ticket)

    def release(self, job_id: str) -> None:
        with self.lock:
            self.jobs.pop(job_id, None)

    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            # Remove abandoned clients, not live waits: every poll renews this timestamp.
            for owner, queue in self.queues.items():
                for ticket, touched in list(queue.items()):
                    if now - touched > 30 and self.permits.get(owner) != ticket:
                        self.cancel(owner, ticket)
            owners = set(self.limits) | {entry.owner for entry in self.jobs.values()}
            for owner in owners:
                failure = None
                for entry in self.jobs.values():
                    if entry.owner != owner:
                        continue
                    try:
                        entry.usage = entry.sample()
                        if entry.usage.get("processes", 0) == 0:
                            entry.stopping = False
                    except Exception as exc:
                        failure = error_report(exc)
                if failure:
                    self.errors[owner] = failure
                    continue
                self.errors.pop(owner, None)
                usage, bounds = self._usage(owner), self._bounds(owner)
                exceeded = [key for key in bounds if usage[key] > bounds[key]]
                if not exceeded or any(e.stopping for e in self.jobs.values() if e.owner == owner):
                    continue
                for entry in reversed(self.jobs.values()):
                    if entry.owner != owner or not entry.usage.get("processes", 0):
                        continue
                    reason = "Model command aggregate resource limit exceeded: " + ", ".join(
                        f"{key}={usage[key]}, limit={bounds[key]}" for key in exceeded
                    )
                    try:
                        entry.terminate(reason)
                        entry.stopping = True
                    except Exception as exc:
                        self.errors[owner] = error_report(exc)
                    break

    def _run(self) -> None:
        while not self._stop.wait(0.25):
            self.tick()

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
