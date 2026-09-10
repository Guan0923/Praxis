"""Persistent local Memory scheduling with a six-hour inactivity gate."""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.domain.memory import MemoryJob, MemoryJobKind, MemoryJobStatus, MemorySettings
from backend.jobs import AdmissionPolicy, JobLane, QueueMode, ThreadJob
from backend.providers import ModelConfigurationError, ModelTransportError
from backend.storage.memory import MemoryConflictError, MemoryNotFoundError

from .consolidation import ManualMemoryConsolidator
from .extraction import ManualEpisodicExtractor
from .provider_models import MemoryModelUnavailable
from .source import MemoryConversation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryAutomationSettings:
    idle_seconds: int = 6 * 60 * 60
    scan_interval_seconds: float = 30.0
    phase1_concurrency: int = 2
    lease_seconds: int = 900
    retry_base_seconds: int = 30
    retry_max_seconds: int = 1800


class MemoryAutomationService:
    def __init__(self, state, model_factory: Callable[[], object], *, settings=None, clock=None) -> None:
        self._state = state
        self._model_factory = model_factory
        self.settings = settings or MemoryAutomationSettings()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active: dict[str, tuple[MemoryJobKind, ThreadJob]] = {}
        self._service_job: ThreadJob | None = None
        self._wake_event = threading.Event()
        self._worker_id = f"memory_worker_{hashlib.sha256(str(id(self)).encode()).hexdigest()[:16]}"

    def start(self) -> None:
        if self._service_job is not None:
            return
        self._service_job = ThreadJob("memory_automation_service", self._run_loop)
        self._state.system_job_scope.submit(
            self._service_job,
            lane=JobLane.SERVICE,
            admission=AdmissionPolicy(queue_mode=QueueMode.REJECT, queue_timeout_seconds=0),
        )

    def close(self) -> None:
        if self._service_job is not None:
            self._service_job.cancel("memory automation closed")
        self._wake_event.set()

    def wake(self) -> None:
        self._wake_event.set()

    def enqueue_extract(self, thread_id: str) -> MemoryJob:
        conversation = self._eligible_conversation(thread_id)
        job = MemoryJob.new(kind=MemoryJobKind.EXTRACT, source_id=thread_id, project_id=conversation.project_id)
        stored, _ = self._state.memory_store.enqueue_job_if_absent(job)
        self.wake()
        return stored

    def enqueue_consolidate(self, *, project_id: str | None = None) -> MemoryJob:
        self._require_enabled()
        job = MemoryJob.new(
            kind=MemoryJobKind.CONSOLIDATE,
            source_id=f"scope_{project_id or 'global'}",
            project_id=project_id,
        )
        stored, _ = self._state.memory_store.enqueue_job_if_absent(job)
        self.wake()
        return stored

    def cancel(self, job_id: str) -> MemoryJob:
        job = self._state.memory_store.cancel_job(job_id, reason="user_cancelled")
        active = self._active.get(job_id)
        if active is not None:
            active[1].cancel("user requested cancellation")
        return job

    def stop(self) -> None:
        for job in self._state.memory_store.list_jobs(limit=1000):
            if job.status not in {MemoryJobStatus.PENDING, MemoryJobStatus.RUNNING}:
                continue
            self._cancel_safely(job.job_id, "memory_disabled")
            active = self._active.get(job.job_id)
            if active is not None:
                active[1].cancel("memory disabled")

    def clear(self) -> None:
        self.stop()
        self._state.memory_store.clear_all()
        self._state.memory_store.rebuild_projections()

    def scan_once(self) -> None:
        if not self._settings().enabled:
            return
        cutoff = self._clock() - timedelta(seconds=self.settings.idle_seconds)
        for conversation in self._state.memory_source.list():
            if (
                conversation.archived
                or conversation.deleted
                or conversation.running
                or conversation.updated_at > cutoff
            ):
                continue
            snapshot = self._state.memory_source.snapshot(conversation)
            watermark = self._state.memory_store.get_watermark(conversation.thread_id)
            if watermark is None or watermark.position < len(snapshot.messages):
                self._state.memory_store.enqueue_job_if_absent(
                    MemoryJob.new(
                        kind=MemoryJobKind.EXTRACT,
                        source_id=conversation.thread_id,
                        project_id=conversation.project_id,
                    )
                )
        self._dispatch()

    def _run_loop(self, *, is_cancelled: Callable[[], bool]) -> None:
        while not is_cancelled():
            try:
                self.scan_once()
            except Exception as exc:
                logger.warning("memory scan failed safely: %s", exc.__class__.__name__)
            self._wake_event.wait(self.settings.scan_interval_seconds)
            self._wake_event.clear()

    def _dispatch(self) -> None:
        extract_active = sum(kind is MemoryJobKind.EXTRACT for kind, _ in self._active.values())
        capacity = self.settings.phase1_concurrency - extract_active
        while capacity > 0:
            job = self._state.memory_store.claim_job(
                self._worker_id,
                kind=MemoryJobKind.EXTRACT,
                lease_seconds=self.settings.lease_seconds,
            )
            if job is None:
                break
            self._submit(job)
            capacity -= 1
        if not any(kind is MemoryJobKind.CONSOLIDATE for kind, _ in self._active.values()):
            job = self._state.memory_store.claim_job(
                self._worker_id,
                kind=MemoryJobKind.CONSOLIDATE,
                lease_seconds=self.settings.lease_seconds,
            )
            if job is not None:
                self._submit(job)

    def _submit(self, job: MemoryJob) -> None:
        carrier = ThreadJob(f"{job.job_id}_attempt_{job.attempts}", self._execute, args=(job,))
        self._active[job.job_id] = (job.kind, carrier)
        try:
            self._state.system_job_scope.submit(
                carrier,
                lane=JobLane.BACKGROUND,
                admission=AdmissionPolicy(queue_mode=QueueMode.REJECT, queue_timeout_seconds=0),
            )
        except Exception as exc:
            self._active.pop(job.job_id, None)
            self._retry(job, exc)

    def _execute(self, job: MemoryJob, *, is_cancelled: Callable[[], bool]) -> None:
        try:
            self._verify_job_active(job.job_id, is_cancelled)
            if job.kind is MemoryJobKind.EXTRACT:
                conversation = self._eligible_conversation(job.source_id or "")
                before = self._source_version(conversation)
                result = ManualEpisodicExtractor.from_settings(
                    self._state.memory_store,
                    self._model_factory(),
                    self._settings(),
                ).extract(
                    self._state.memory_source.snapshot(conversation),
                    before_commit=lambda: self._verify_extract_commit(
                        job.job_id,
                        conversation.thread_id,
                        before,
                        is_cancelled,
                    ),
                    job_id=job.job_id,
                    lease_owner=self._worker_id,
                )
                if result.model_called and result.records:
                    self.enqueue_consolidate(project_id=conversation.project_id)
            else:
                ManualMemoryConsolidator.from_settings(
                    self._state.memory_store,
                    self._model_factory(),
                    self._settings(),
                ).consolidate(
                    project_id=job.project_id,
                    before_commit=lambda: self._verify_job_active(job.job_id, is_cancelled),
                    job_id=job.job_id,
                    lease_owner=self._worker_id,
                )
            self._verify_job_active(job.job_id, is_cancelled)
            self._state.memory_store.complete_job(job.job_id, self._worker_id)
        except (_MemoryWorkCancelled, _MemoryConversationIneligible):
            self._cancel_safely(job.job_id, "conversation_changed")
        except MemoryModelUnavailable as exc:
            self._cancel_safely(job.job_id, str(exc))
        except ModelConfigurationError:
            self._cancel_safely(job.job_id, "provider_unavailable")
        except ModelTransportError as exc:
            if exc.status_code in {402, 429}:
                self._cancel_safely(job.job_id, "quota_unavailable")
            elif exc.status_code in {401, 403, 404}:
                self._cancel_safely(job.job_id, "provider_unavailable")
            else:
                self._retry(job, exc)
        except MemoryConflictError:
            pass
        except Exception as exc:
            self._retry(job, exc)
        finally:
            self._active.pop(job.job_id, None)

    def _eligible_conversation(self, thread_id: str) -> MemoryConversation:
        self._require_enabled()
        conversation = self._state.memory_source.get(thread_id)
        if conversation is None or conversation.archived or conversation.deleted:
            raise _MemoryConversationIneligible("Conversation is in the recycle bin or does not exist.")
        if conversation.running:
            raise _MemoryConversationIneligible("Conversation is still running.")
        if conversation.updated_at > self._clock() - timedelta(seconds=self.settings.idle_seconds):
            raise _MemoryConversationIneligible("Conversation must be inactive for at least 6 hours.")
        return conversation

    def _source_version(self, conversation: MemoryConversation) -> tuple[datetime, tuple[str, ...]]:
        snapshot = self._state.memory_source.snapshot(conversation)
        return conversation.updated_at, tuple(message.source_id for message in snapshot.messages)

    def _verify_unchanged(
        self,
        thread_id: str,
        expected: tuple[datetime, tuple[str, ...]],
        is_cancelled: Callable[[], bool],
    ) -> None:
        current = self._eligible_conversation(thread_id)
        if is_cancelled() or self._source_version(current) != expected:
            raise _MemoryWorkCancelled()

    def _verify_extract_commit(
        self,
        job_id: str,
        thread_id: str,
        expected: tuple[datetime, tuple[str, ...]],
        is_cancelled: Callable[[], bool],
    ) -> None:
        self._verify_job_active(job_id, is_cancelled)
        self._verify_unchanged(thread_id, expected, is_cancelled)

    def _verify_job_active(self, job_id: str, is_cancelled: Callable[[], bool]) -> None:
        if is_cancelled() or not self._settings().enabled:
            raise _MemoryWorkCancelled()
        current = self._state.memory_store.get_job(job_id)
        if current is None or current.status is not MemoryJobStatus.RUNNING:
            raise _MemoryWorkCancelled()

    def _settings(self) -> MemorySettings:
        return MemorySettings.from_mapping(self._state.settings.memory_config())

    def _require_enabled(self) -> None:
        if not self._settings().enabled:
            raise ValueError("Memory is disabled.")

    def _cancel_safely(self, job_id: str, reason: str) -> None:
        try:
            self._state.memory_store.cancel_job(job_id, reason=reason)
        except (MemoryConflictError, MemoryNotFoundError):
            pass

    def _retry(self, job: MemoryJob, exc: Exception) -> None:
        delay = min(
            self.settings.retry_max_seconds,
            self.settings.retry_base_seconds * (2 ** max(job.attempts - 1, 0)),
        )
        retry_at = (self._clock() + timedelta(seconds=delay)).isoformat()
        try:
            self._state.memory_store.fail_job(
                job.job_id,
                self._worker_id,
                f"{exc.__class__.__name__}:{str(exc)[:200]}",
                retry_at=retry_at,
            )
        except (MemoryConflictError, MemoryNotFoundError):
            pass
        logger.warning("memory job failed safely: %s", exc.__class__.__name__)


class _MemoryWorkCancelled(RuntimeError):
    pass


class _MemoryConversationIneligible(ValueError):
    pass


__all__ = ["MemoryAutomationService", "MemoryAutomationSettings"]
