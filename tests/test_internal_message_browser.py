"""Real local HTTP model, backend, SSE and browser; independent data and ports."""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import pytest
import uvicorn

from backend.api.app import create_app
from backend.api.state import WebAppState
from tests import test_agent_threads


@pytest.fixture
def local_model():
    yield from test_agent_threads.local_subagent_model.__wrapped__()


def test_browser_sends_and_refreshes_unchanged_wire_messages(tmp_path, monkeypatch, local_model, local_sandbox_runtime):
    root = Path(__file__).resolve().parents[1]
    if not (root / "frontend/dist/index.html").exists() or not shutil.which("node"):
        pytest.skip("Build frontend and install Playwright first")
    model, calls = local_model

    class Broker:
        def status(self):
            return {"installed": True, "healthy": True, "service_state": "running", "code": "ready"}

    state = WebAppState(tmp_path / "data", sandbox_broker=Broker())
    state.settings.update_provider_config(
        {
            "provider_name": model.provider_name,
            "protocol": "chat_completions",
            "base_url": model.base_url,
            "model": model.model,
            "api_key": "local-test-only",
            "max_tokens": 256,
            "context_size": 128000,
        }
    )
    sid = state.session_store.create_session("Flow browser test").session_id
    state.session_store.create_sidebar_thread(session_id=sid, thread_id=sid, title="Flow browser test")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setenv("PRAXIS_FRONTEND_DIST", str(root / "frontend/dist"))
    monkeypatch.setenv("PRAXIS_ALLOWED_ORIGINS", f"http://127.0.0.1:{port}")
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="error"))
    worker = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    worker.start()
    try:
        deadline = monotonic() + 10
        while not server.started and monotonic() < deadline:
            sleep(0.02)
        result = subprocess.run(
            [
                shutil.which("node"),
                str(root / "tests/support/internal_message_check.cjs"),
                f"http://127.0.0.1:{port}",
                str(tmp_path),
                sid,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-6000:]
        assert len(calls) >= 2
        nodes = [
            node
            for node in state.session_store.load_nodes(sid)
            if node.id != sid and getattr(node, "status", None) == "success"
        ]
        assert len(nodes) >= 2
        print(result.stdout)
    finally:
        server.should_exit = True
        worker.join(10)
        listener.close()
        state.close()
