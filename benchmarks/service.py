"""Process-local benchmark queue and result projection, owned by the Web app."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from time import monotonic
from uuid import uuid4

from backend.domain import safe_error_message
from backend.jobs import (
    TERMINAL_STATES,
    AdmissionPolicy,
    JobLane,
    JobRegistry,
    JobScopeKind,
    JobState,
    JobStateChange,
    QueueMode,
    SlotMode,
    ThreadJob,
)
from backend.providers import ModelConfig
from backend.runtime.core.events import RuntimeEvent

from .model import BenchmarkTask, TaskResult
from .resources import ResourceStore, resource_store
from .runner import _error_result, run_one_task
from .sandbox import Sandbox

FINISHED = frozenset({"completed", "failed", "cancelled"})


class BenchmarkConflict(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class TaskRun:
    id: str
    task: BenchmarkTask
    planner: str
    model_config: ModelConfig | None = field(repr=False)
    status: str = "queued"
    phase: str = "queued"
    activity: str = "queued"
    updated_at: str = field(default_factory=_now)
    started: float | None = None
    finished: float | None = None
    result: TaskResult | None = None
    job: ThreadJob | None = None


@dataclass
class BatchRun:
    id: str
    tasks: list[TaskRun]
    created_at: str = field(default_factory=_now)


class BenchmarkService:
    """At most three isolated task workers; no provider calls on request threads."""

    def __init__(
        self,
        registry: JobRegistry,
        root: Path,
        *,
        execute: Callable[..., TaskResult] = run_one_task,
        resources: ResourceStore | None = None,
    ) -> None:
        self.instance_id = uuid4().hex
        self._root = root
        self._execute = execute
        self.resources = resources or resource_store()
        self._scope = registry.root_scope().child(JobScopeKind.TASK)
        self._lock = RLock()
        self._runs: dict[str, BatchRun] = {}
        self._jobs: dict[str, TaskRun] = {}
        self._pending: deque[TaskRun] = deque()
        self._running: set[str] = set()
        self._closed = False

    def start(self, tasks: Sequence[BenchmarkTask], planner: str, model_config: ModelConfig | None) -> dict:
        with self._lock:
            if self._closed:
                raise BenchmarkConflict("Benchmark service is shutting down.")
            if not tasks or any(planner not in task.planner_modes for task in tasks):
                raise ValueError("No tasks support the requested planner.")
            active = {
                item.task.name for batch in self._runs.values() for item in batch.tasks if item.status not in FINISHED
            }
            conflicts = sorted(active.intersection(task.name for task in tasks))
            if conflicts:
                raise BenchmarkConflict("Tasks already active: " + ", ".join(conflicts))
            self.resources.reserve(tasks)
            batch = BatchRun(uuid4().hex, [TaskRun(uuid4().hex, task, planner, model_config) for task in tasks])
            self._runs[batch.id] = batch
            self._pending.extend(batch.tasks)
            self._dispatch()
            return self._batch_snapshot(batch)

    def _dispatch(self) -> None:
        while not self._closed and self._pending and len(self._running) < 3:
            item = self._pending.popleft()
            if item.status != "queued":
                continue
            item.status = "running"
            item.started = monotonic()
            self._running.add(item.id)
            job = ThreadJob(self._scope.registry.new_job_id(), self._worker, args=(item,))
            item.job = job
            self._jobs[job.info().id] = item
            job.add_listener(self)
            try:
                # The benchmark queue owns its three slots; do not consume or
                # change the app's shared background lane limit of two.
                self._scope.submit(
                    job,
                    lane=JobLane.BACKGROUND,
                    admission=AdmissionPolicy(queue_mode=QueueMode.REJECT, slot_mode=SlotMode.UNMETERED),
                )
            except Exception as exc:
                item.result = _error_result(item.task, safe_error_message(exc), failure_phase="application")
                self._finish(item, "failed")

    def _worker(self, item: TaskRun, *, is_cancelled: Callable[[], bool]) -> None:
        def phase(value: str) -> None:
            with self._lock:
                item.phase = value
                item.activity = value
                item.updated_at = _now()

        def event(value: RuntimeEvent) -> None:
            with self._lock:
                item.activity = value.kind
                item.updated_at = _now()

        try:
            phase("workspace")
            sandbox = Sandbox(self._root / item.id, model_config=item.model_config)
            sandbox.prepare()
            if not is_cancelled():
                result = self._execute(
                    item.task,
                    planner=item.planner,
                    sandbox=sandbox,
                    cancel_requested=is_cancelled,
                    on_phase=phase,
                    on_event=event,
                    resource_cache=self.resources.cache,
                )
                with self._lock:
                    item.result = result
        except Exception as exc:
            with self._lock:
                item.result = _error_result(item.task, safe_error_message(exc), failure_phase=item.phase)
        finally:
            with self._lock:
                item.model_config = None

    def on_job_state_change(self, change: JobStateChange) -> None:
        if change.job_info.state not in TERMINAL_STATES:
            if change.job_info.cancel_requested_at is not None:
                with self._lock:
                    item = self._jobs.get(change.job_info.id)
                    if item is not None and item.status not in FINISHED:
                        item.status = "stopping"
            return
        with self._lock:
            item = self._jobs.get(change.job_info.id)
            if item is None or item.status in FINISHED:
                return
            result = item.result
            if change.job_info.state == JobState.FAILED or (result is not None and result.status == "error"):
                if result is None:
                    item.result = _error_result(
                        item.task, change.job_info.error or "Worker failed.", failure_phase=item.phase
                    )
                status = "failed"
            elif change.job_info.state == JobState.CANCELLED:
                status = "cancelled"
            elif result is not None and result.status == "completed":
                status = "completed"
            else:
                status = "failed"
            self._finish(item, status)
            self._dispatch()

    def _finish(self, item: TaskRun, status: str) -> None:
        self.resources.release(item.task)
        item.status = status
        item.finished = monotonic()
        item.updated_at = _now()
        item.model_config = None
        self._running.discard(item.id)

    def resource_operation(self, task: BenchmarkTask, action: str) -> dict:
        with self._lock:
            if self._closed:
                raise BenchmarkConflict("Benchmark service is shutting down.")
            return self.resources.submit(task, action, self._scope)

    def snapshot(self, run_id: str | None = None) -> dict:
        with self._lock:
            if run_id is not None:
                return self._batch_snapshot(self._runs[run_id])
            return {"instance_id": self.instance_id, "runs": [self._batch_snapshot(run) for run in self._runs.values()]}

    def trace(self, run_id: str, task_id: str) -> list[dict]:
        with self._lock:
            item = self._task(self._runs[run_id], task_id)
            return list(item.result.trace) if item.result is not None else []

    @staticmethod
    def _task(batch: BatchRun, task_id: str) -> TaskRun:
        for item in batch.tasks:
            if item.id == task_id:
                return item
        raise KeyError(task_id)

    def cancel(self, run_id: str, task_id: str | None = None) -> dict:
        with self._lock:
            batch = self._runs[run_id]
            items = batch.tasks if task_id is None else [self._task(batch, task_id)]
            for item in items:
                if item.status == "queued":
                    self._finish(item, "cancelled")
                elif item.status not in FINISHED and item.job is not None:
                    item.status = "stopping"
                    item.updated_at = _now()
                    item.job.cancel()
            self._dispatch()
            return self._batch_snapshot(batch)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for run_id in self._runs:
                self.cancel(run_id)
        self._scope.close(timeout=5.0)

    def _batch_snapshot(self, batch: BatchRun) -> dict:
        tasks = [self._task_snapshot(item) for item in batch.tasks]
        statuses = {item.status for item in batch.tasks}
        if "stopping" in statuses:
            status = "stopping"
        elif "running" in statuses:
            status = "running"
        elif "queued" in statuses:
            status = "queued"
        elif "failed" in statuses:
            status = "failed"
        elif "cancelled" in statuses:
            status = "cancelled"
        else:
            status = "completed"
        return {
            "id": batch.id,
            "instance_id": self.instance_id,
            "created_at": batch.created_at,
            "status": status,
            "total": len(tasks),
            "finished": sum(item.status in FINISHED for item in batch.tasks),
            "tasks": tasks,
        }

    @staticmethod
    def _task_snapshot(item: TaskRun) -> dict:
        result = None
        if item.result is not None and item.status in FINISHED:
            # Do not serialize the large trace on each polling request.
            result = item.result.to_dict()
            result.pop("trace")
        return {
            "id": item.id,
            "task_name": item.task.name,
            "status": item.status,
            "phase": item.phase,
            "activity": item.activity,
            "updated_at": item.updated_at,
            "duration_ms": round(((item.finished or monotonic()) - item.started) * 1000, 3) if item.started else 0,
            "result": result,
            "trace_count": len(item.result.trace) if result is not None else 0,
        }
