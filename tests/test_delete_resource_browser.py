"""Isolated HTTP/SQLite/browser verification; no installed Broker or model API."""

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import APIRouter

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.sandbox.runtime.aggregate import AggregateResources


def test_delete_and_resource_settings_in_browser(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if not (root / "frontend/dist/index.html").exists() or not shutil.which("node"):
        pytest.skip("Build frontend first")
    resources = AggregateResources(start=False)
    recovery_probe = {"failed": False, "calls": 0}

    class Broker:
        def status(self):
            return {
                "installed": True,
                "healthy": not recovery_probe["failed"],
                "service_state": "stopped" if recovery_probe["failed"] else "running",
                "code": "broker_service_not_running" if recovery_probe["failed"] else "ready",
                "detail": "Isolated test",
            }

        def repair(self, *, before_repair=None):
            recovery_probe["calls"] += 1
            error = FileNotFoundError(2, "test sandbox executable missing")
            error.broker_recovery_code = "broker_service_start_failed"
            raise error

        def resource_request(self, operation, **values):
            if operation == "resource_configure":
                resources.configure("test", values["limits"])
            return resources.status("test")

    state = WebAppState(tmp_path / "data", sandbox_broker=Broker())
    state.turn_message_worker.close()
    for title in ("Delete desktop", "Delete mobile", "Keep history"):
        sid = state.session_store.create_session(title).session_id
        state.session_store.create_sidebar_thread(session_id=sid, thread_id=sid, title=title)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setenv("PRAXIS_FRONTEND_DIST", str(root / "frontend/dist"))
    monkeypatch.setenv("PRAXIS_ALLOWED_ORIGINS", f"http://127.0.0.1:{port}")
    app = create_app(state)

    probe_routes = APIRouter()

    @probe_routes.post("/test/start-failure")
    def start_failure():
        recovery_probe["failed"] = True
        return recovery_probe

    @probe_routes.get("/test/recovery-count")
    def recovery_count():
        return recovery_probe

    app.router.routes[0:0] = probe_routes.routes

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        result = subprocess.run(
            [
                shutil.which("node"),
                str(root / "tests/support/delete_resource_check.cjs"),
                f"http://127.0.0.1:{port}",
                str(tmp_path),
            ],
            cwd=root / "frontend",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
        assert result.returncode == 0, result.stderr[-6000:]
        print(result.stdout)
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        state.close()
        resources.close()
