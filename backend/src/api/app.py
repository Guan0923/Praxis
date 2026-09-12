"""FastAPI application: chat with the agent, plus an optional benchmark sub-app.

The main chat backend never imports the benchmark harness; benchmark routes
live in a separately mounted sub-application under /benchmark.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .error_handlers import install_error_handlers
from .security import LocalWebSettings, origin_allowed
from .state import DEFAULT_DATA_ROOT, WebAppState

REPO_ROOT = Path(__file__).resolve().parents[3]


def create_app(state: WebAppState | None = None) -> FastAPI:
    resolved = state or WebAppState(DEFAULT_DATA_ROOT)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            resolved.close()

    app = FastAPI(title="Praxis Web", version="0.0.1", lifespan=lifespan)
    install_error_handlers(app)
    app.state.web = resolved
    web_settings = LocalWebSettings.from_env()

    @app.middleware("http")
    async def enforce_local_browser_origin(request: Request, call_next):
        if resolved.closing:
            return JSONResponse({"detail": "Backend is shutting down."}, status_code=503)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not origin_allowed(request, web_settings):
            return JSONResponse(
                {"detail": "不允许的请求来源。"},
                status_code=403,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        from .operation_control import canonical_operation_session, operation_headers

        try:
            operation = operation_headers(request) if request.method not in {"GET", "HEAD", "OPTIONS"} else None
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if operation is None and request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin"):
            return JSONResponse({"detail": "浏览器写请求缺少操作协议头。"}, status_code=400)
        if operation is None:
            response = await call_next(request)
        else:
            window_id, generation, group, session_id, seq, ack = operation
            try:
                session_id = await canonical_operation_session(request, resolved, session_id)
                state, lock = await resolved.operation_control.operation(
                    window_id=window_id,
                    generation=generation,
                    group=group,
                    session_id=session_id,
                )
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            try:
                async with lock:
                    if seq != state.client_seq or ack != state.server_seq:
                        return JSONResponse(
                            {"detail": "操作序号不匹配，未执行请求。", "code": "operation_sequence_mismatch"},
                            status_code=409,
                            headers={
                                "X-Praxis-Seq": str(state.server_seq),
                                "X-Praxis-Ack": str(state.client_seq),
                            },
                        )
                    response = await call_next(request)
                    response.headers["X-Praxis-Seq"] = str(state.server_seq)
                    state.client_seq += 1
                    state.server_seq += 1
                    response.headers["X-Praxis-Ack"] = str(state.client_seq)
            finally:
                await resolved.operation_control.finish_operation(window_id, session_id)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(web_settings.allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    from .chat.decisions import router as decisions_router
    from .memory_routes import router as memory_router
    from .routes.agent_threads import router as agent_threads_router
    from .routes.jobs import router as jobs_router
    from .routes.mcp_settings import router as mcp_settings_router
    from .routes.projects import router as projects_router
    from .routes.right_panel import router as right_panel_router
    from .routes.sandbox import router as sandbox_router
    from .routes.settings import router as settings_router
    from .routes.sidebar_threads import router as sidebar_threads_router
    from .routes.skill_settings import router as skill_settings_router
    from .routes.turns import router as turns_router
    from .routes.view_state import router as view_state_router
    from .routes.window_control import router as window_control_router
    from .session_files import router as session_files_router
    from .shared.benchmark import create_benchmark_app
    from .shared.info import router as info_router

    app.include_router(settings_router)
    app.include_router(skill_settings_router)
    app.include_router(mcp_settings_router)
    app.include_router(memory_router)
    app.include_router(agent_threads_router)
    app.include_router(decisions_router)
    app.include_router(jobs_router)
    app.include_router(sandbox_router)
    app.include_router(projects_router)
    app.include_router(right_panel_router)
    app.include_router(info_router)
    app.include_router(sidebar_threads_router)
    app.include_router(turns_router)
    app.include_router(session_files_router)
    app.include_router(window_control_router)
    app.include_router(view_state_router)
    frontend_dist = Path(os.environ.get("PRAXIS_FRONTEND_DIST", str(REPO_ROOT / "frontend" / "dist"))).expanduser()
    if frontend_dist.is_dir():

        @app.get("/benchmark", include_in_schema=False)
        @app.get("/trash", include_in_schema=False)
        @app.get("/chat/{session_id}/{thread_id}", include_in_schema=False)
        @app.get("/chat/{session_id}/{thread_id}/agent/{agent_thread_id}", include_in_schema=False)
        def frontend_page():
            return FileResponse(frontend_dist / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/benchmark", create_benchmark_app(resolved))

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "service": "praxis-backend"}

    @app.get("/api/ready")
    def ready() -> dict:
        resolved.settings.ping()
        resolved.projects.list("all")
        return {"status": "ready", "service": "praxis-backend", "database": "ok"}

    @app.api_route(
        "/api/{missing_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def missing_api(missing_path: str) -> None:
        del missing_path
        raise HTTPException(status_code=404, detail="Not Found")

    # In production the local backend can serve the browser bundle from the
    # same loopback origin.  Development keeps using Vite's proxy, and an
    # absent ``dist`` directory simply leaves the API-only app unchanged.
    frontend_dist = Path(os.environ.get("PRAXIS_FRONTEND_DIST", str(REPO_ROOT / "frontend" / "dist"))).expanduser()
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app
