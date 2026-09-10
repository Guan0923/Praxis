"""Local single-user Memory management routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.api.error_handlers import error_response
from backend.domain.memory import MemoryItem, MemoryJob, MemoryJobStatus, MemorySettings
from backend.runtime.memory import MemoryContextSelector
from backend.storage.memory import MemoryConflictError, MemoryNotFoundError, MemoryStorageError

router = APIRouter(prefix="/api/memory", tags=["memory"])


class ExtractRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=200)


class ConsolidateRequest(BaseModel):
    project_id: str | None = Field(default=None, max_length=200)


class ItemEnabledRequest(BaseModel):
    enabled: bool


class ClearRequest(BaseModel):
    confirm: str


@router.get("/items")
def list_items(
    request: Request,
    project_id: str | None = None,
    include_deleted: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, object]:
    _require_project(request, project_id)
    try:
        if project_id is None:
            project_ids = [project.project_id for project in request.app.state.web.projects.list("active")]
            items = request.app.state.web.memory_store.list_accessible_items(
                project_ids, include_deleted=include_deleted, limit=limit
            )
        else:
            items = request.app.state.web.memory_store.list_items(
                project_id=project_id, include_deleted=include_deleted, limit=limit
            )
    except (MemoryStorageError, ValueError) as exc:
        return error_response(exc, status_code=_memory_error(exc).status_code)
    return {"items": [_item_payload(item) for item in items]}


@router.get("/items/{memory_id}/evidence")
def list_evidence(
    memory_id: str, request: Request, limit: int = Query(default=100, ge=1, le=1000)
) -> dict[str, object]:
    item = _item(request, memory_id)
    values = request.app.state.web.memory_store.list_evidence(memory_id=item.memory_id)[:limit]
    return {"evidence": [value.to_dict() for value in values]}


@router.get("/retrieval/dry-run")
def dry_run(query: str, request: Request, project_id: str | None = None) -> dict[str, object]:
    _require_project(request, project_id)
    settings = MemorySettings.from_mapping(request.app.state.web.settings.memory_config())
    try:
        result = MemoryContextSelector(request.app.state.web.memory_store, settings).select(
            query, project_id=project_id
        )
    except (MemoryStorageError, ValueError) as exc:
        return error_response(exc, status_code=_memory_error(exc).status_code)
    return {
        "enabled": settings.enabled,
        "would_inject": settings.enabled and bool(result.context),
        "context": result.context,
        "result": result.diagnostic(operation="dry_run", injected=bool(result.context), enabled=settings.enabled),
    }


@router.get("/retrieval/latest")
def latest_retrieval(session_id: str, request: Request) -> dict[str, object]:
    return {"record": request.app.state.web.memory_diagnostics.latest("local", session_id)}


@router.get("/retrieval/history")
def retrieval_history(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, object]:
    return {"records": request.app.state.web.memory_diagnostics.list_latest("local", limit=limit)}


@router.get("/jobs")
def list_jobs(
    request: Request, status: MemoryJobStatus | None = None, limit: int = Query(default=100, ge=1, le=1000)
) -> dict[str, object]:
    return {
        "jobs": [_job_payload(job) for job in request.app.state.web.memory_store.list_jobs(status=status, limit=limit)]
    }


@router.post("/extract", status_code=202)
def extract(body: ExtractRequest, request: Request) -> dict[str, object]:
    try:
        return {"job": _job_payload(request.app.state.web.memory_automation.enqueue_extract(body.thread_id))}
    except (MemoryStorageError, ValueError) as exc:
        return error_response(exc, status_code=_memory_error(exc).status_code)


@router.post("/consolidate", status_code=202)
def consolidate(body: ConsolidateRequest, request: Request) -> dict[str, object]:
    _require_project(request, body.project_id)
    try:
        job = request.app.state.web.memory_automation.enqueue_consolidate(project_id=body.project_id)
        return {"job": _job_payload(job)}
    except (MemoryStorageError, ValueError) as exc:
        return error_response(exc, status_code=_memory_error(exc).status_code)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict[str, object]:
    try:
        return {"job": _job_payload(request.app.state.web.memory_automation.cancel(job_id))}
    except (MemoryStorageError, ValueError) as exc:
        return error_response(exc, status_code=_memory_error(exc).status_code)


@router.patch("/items/{memory_id}")
def set_item_enabled(memory_id: str, body: ItemEnabledRequest, request: Request) -> dict[str, object]:
    item = _item(request, memory_id)
    return {
        "memory": _item_payload(
            request.app.state.web.memory_store.set_item_enabled(item.memory_id, enabled=body.enabled)
        )
    }


@router.delete("/items/{memory_id}")
def delete_item(memory_id: str, request: Request) -> dict[str, object]:
    return {
        "memory": _item_payload(request.app.state.web.memory_store.delete_item(_item(request, memory_id).memory_id))
    }


@router.post("/items/{memory_id}/restore")
def restore_item(memory_id: str, request: Request) -> dict[str, object]:
    return {
        "memory": _item_payload(request.app.state.web.memory_store.restore_item(_item(request, memory_id).memory_id))
    }


@router.post("/clear")
def clear(body: ClearRequest, request: Request) -> dict[str, bool]:
    if body.confirm != "CLEAR ALL MEMORIES":
        raise HTTPException(status_code=422, detail="Clear confirmation text does not match.")
    request.app.state.web.memory_automation.clear()
    return {"cleared": True}


def _require_project(request: Request, project_id: str | None) -> None:
    if project_id is not None and request.app.state.web.projects.get(project_id, include_removed=False) is None:
        raise HTTPException(status_code=404, detail="Project does not exist.")


def _item(request: Request, memory_id: str) -> MemoryItem:
    item = request.app.state.web.memory_store.get_item(memory_id, include_deleted=True)
    if item is None:
        raise HTTPException(status_code=404, detail="Memory item does not exist.")
    if (
        item.project_id is not None
        and request.app.state.web.projects.get(item.project_id, include_removed=False) is None
    ):
        raise HTTPException(status_code=404, detail="Memory item does not exist in an active project.")
    return item


def _item_payload(item: MemoryItem) -> dict[str, object]:
    return item.to_dict()


def _job_payload(job: MemoryJob) -> dict[str, object]:
    return job.to_dict()


def _memory_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, MemoryConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=503, detail="Memory storage is temporarily unavailable.")


__all__ = ["router"]
