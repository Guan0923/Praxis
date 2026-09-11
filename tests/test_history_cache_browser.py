"""Real loopback SQLite history and browser pagination, without a model."""

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.domain.runtime_state import RuntimeState


def test_history_paging_in_real_browser(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if not (root / "frontend/dist/index.html").exists() or not shutil.which("node"):
        pytest.skip("Build frontend and install Node for browser verification")

    class TestBroker:
        def status(self):
            return {"installed": True, "healthy": True, "code": "ready", "detail": "Isolated history test"}

    state = WebAppState(tmp_path / "data", sandbox_broker=TestBroker())
    state.turn_message_worker.close()
    store = state.session_store
    session = store.create_session("Cache history")
    sid = session.session_id
    store.create_sidebar_thread(session_id=sid, thread_id=sid, title="Cache history")
    parent = store.ensure_root_node(sid)
    for number in range(12):
        turn = RuntimeState.create(
            session_id=sid,
            thread_id=sid,
            id=f"history-{number}",
            provider_name="local-test",
            cwd=str(tmp_path),
            parent=parent,
            user_content=[{"type": "text", "text": f"HISTORY_{number:02}", "status": "success"}],
        )
        store.create_node(turn)
        turn.data[0].append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": f"Reply {number}\n\n" + "Local history verification. " * 20,
                        "status": "success",
                    }
                ],
            }
        )
        turn.status = "success"
        store.finalize_node(turn)
        parent = turn
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setenv("PRAXIS_FRONTEND_DIST", str(root / "frontend/dist"))
    monkeypatch.setenv("PRAXIS_ALLOWED_ORIGINS", f"http://127.0.0.1:{port}")
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="error"))
    serving = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    serving.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        result = subprocess.run(
            [
                shutil.which("node"),
                str(root / "tests/support/history_cache_check.cjs"),
                f"http://127.0.0.1:{port}",
                str(tmp_path),
                sid,
            ],
            cwd=root / "frontend",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
        assert result.returncode == 0, result.stderr[-5000:]
        print(result.stdout)
    finally:
        server.should_exit = True
        serving.join(10)
        listener.close()
        state.close()
