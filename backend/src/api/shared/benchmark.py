"""Benchmark harness as a separately mounted FastAPI sub-application.

Mounted at /benchmark by the main app. It imports the benchmark harness lazily
inside handlers, so starting the chat backend never pulls in the benchmark code.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..error_handlers import error_response, install_error_handlers
from ..jsonl import jsonl_download
from ..state import WebAppState

if TYPE_CHECKING:
    from benchmarks.model import BenchmarkTask
    from benchmarks.service import BenchmarkService


class RunRequest(BaseModel):
    task: str
    planner: str = "llm"


class RunAllRequest(BaseModel):
    planner: str = "llm"


def _service(request: Request) -> BenchmarkService:
    from benchmarks.service import BenchmarkService

    web: WebAppState = request.app.state.web
    with web.benchmark_lock:
        if web.benchmark_service is None:
            web.benchmark_service = BenchmarkService(web.job_registry, web.benchmark_data_root())
        return web.benchmark_service


def _start(request: Request, tasks: Sequence[BenchmarkTask], planner: str) -> dict:
    from benchmarks.resources import ResourceConflict
    from benchmarks.service import BenchmarkConflict

    if not tasks or any(planner not in task.planner_modes for task in tasks):
        raise HTTPException(status_code=422, detail="No tasks support the requested planner.")
    service = _service(request)
    try:
        service.resources.ensure_ready(tasks)
    except ResourceConflict as exc:
        return error_response(exc, status_code=409)
    try:
        model_config = request.app.state.web.model_config()
    except Exception as exc:
        return error_response(exc, status_code=422)
    try:
        return service.start(tasks, planner, model_config)
    except (BenchmarkConflict, ResourceConflict) as exc:
        return error_response(exc, status_code=409)
    except ValueError as exc:
        return error_response(exc, status_code=422)


# Mounted at /benchmark by the main app, so the router carries no prefix.
router = APIRouter()


@router.get("/resources")
def list_resources(request: Request) -> list[dict]:
    from benchmarks.tasks import ALL_TASKS

    return _service(request).resources.snapshot(ALL_TASKS)


def _resource_operation(name: str, action: str, request: Request) -> dict:
    from benchmarks.resources import ResourceConflict
    from benchmarks.service import BenchmarkConflict
    from benchmarks.tasks import TASKS_BY_NAME

    task = TASKS_BY_NAME.get(name)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Unknown benchmark task: {name}")
    try:
        return _service(request).resource_operation(task, action)
    except (ResourceConflict, BenchmarkConflict) as exc:
        return error_response(exc, status_code=409)


@router.post("/tasks/{name}/resources", status_code=202)
def prepare_resources(name: str, request: Request) -> dict:
    return _resource_operation(name, "prepare", request)


@router.delete("/tasks/{name}/resources", status_code=202)
def delete_resources(name: str, request: Request) -> dict:
    return _resource_operation(name, "delete", request)


@router.get("/tasks")
def list_tasks(request: Request) -> list[dict]:
    from benchmarks.containers import image_status
    from benchmarks.tasks import ALL_TASKS

    return [
        {
            "name": task.name,
            "capability": task.capability,
            "description": task.description,
            "prompt": task.prompt,
            "difficulty": task.difficulty,
            "budgets": {
                "max_tool_calls": task.budgets.max_tool_calls,
                "timeout_seconds": task.budgets.timeout_seconds,
            },
            "suite_version": task.suite_version,
            "environment": {"kind": "docker" if task.container else "local", "status": image_status(task)},
            "tags": list(task.tags),
            "source": {
                "benchmark": task.source.benchmark,
                "task_id": task.source.task_id,
                "url": task.source.url,
                "source_revision": task.source.source_revision,
                "license": task.source.license,
                "adaptation_notes": task.source.adaptation_notes,
            },
            "planner_modes": sorted(task.planner_modes),
        }
        for task in ALL_TASKS
    ]


@router.post("/run", status_code=202)
def run_benchmark(body: RunRequest, request: Request) -> dict:
    from benchmarks.tasks import TASKS_BY_NAME

    task = TASKS_BY_NAME.get(body.task)
    if task is None:
        raise HTTPException(status_code=404, detail=f"未知任务：{body.task}")
    return _start(request, [task], body.planner)


@router.post("/run-all", status_code=202)
def run_all_benchmark(body: RunAllRequest, request: Request) -> dict:
    from benchmarks.tasks import ALL_TASKS

    return _start(request, ALL_TASKS, body.planner)


@router.get("/runs")
def list_runs(request: Request) -> dict:
    return _service(request).snapshot()


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    try:
        return _service(request).snapshot(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="运行记录不存在或已失效。") from None


@router.get("/runs/{run_id}/tasks/{task_id}/trace")
def get_trace(run_id: str, task_id: str, request: Request) -> list[dict]:
    try:
        return _service(request).trace(run_id, task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="运行记录不存在或已失效。") from None


@router.get("/runs/{run_id}/tasks/{task_id}/trace/export")
def export_trace(run_id: str, task_id: str, request: Request) -> StreamingResponse:
    return jsonl_download(get_trace(run_id, task_id, request), f"benchmark-{run_id}-{task_id}-trace.jsonl")


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str, request: Request) -> dict:
    return _cancel(request, run_id)


@router.post("/runs/{run_id}/tasks/{task_id}/cancel", status_code=202)
def cancel_task(run_id: str, task_id: str, request: Request) -> dict:
    return _cancel(request, run_id, task_id)


def _cancel(request: Request, run_id: str, task_id: str | None = None) -> dict:
    try:
        return _service(request).cancel(run_id, task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="运行记录不存在或已失效。") from None


def create_benchmark_app(web_state: WebAppState) -> FastAPI:
    app = FastAPI(title="Praxis Benchmark", version="0.0.1")
    install_error_handlers(app)
    app.state.web = web_state
    app.include_router(router)
    return app
