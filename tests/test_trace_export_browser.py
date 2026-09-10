"""Real loopback HTTP and Chromium downloads using an isolated local data root."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from backend.api.app import create_app
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.storage.message_queue import MemoryMessageQueue
from tests.test_benchmark_service import until
from tests.test_trace_export import seed_benchmark, seed_thread

ROOT = Path(__file__).resolve().parents[1]


def export_preview_app(data_root: Path):
    web = WebAppState(data_root, message_queue=MemoryMessageQueue())
    # This test does not execute tools or change the machine-wide Broker.
    web.sandbox_broker = SimpleNamespace(
        status=lambda: {"installed": True, "healthy": True, "code": None, "detail": None},
    )
    session_id, turns = seed_thread(web)
    last = turns[-1]
    last.status = "success"
    session_store(web).update_node(last)
    run_id, task_id, events = seed_benchmark(web, session_id=session_id)
    return create_app(web), session_id, run_id, task_id, events


@pytest.mark.skipif(
    not (ROOT / "frontend/dist/index.html").is_file() or not (ROOT / "frontend/node_modules/playwright").is_dir(),
    reason="Build the frontend and install its dependencies before browser download acceptance.",
)
def test_real_browser_downloads_and_responsive_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAXIS_FRONTEND_DIST", str(ROOT / "frontend/dist"))
    app, session_id, run_id, task_id, events = export_preview_app(tmp_path / "web")
    trace_requests = []

    @app.middleware("http")
    async def record_download_requests(request, call_next):
        if request.url.path.startswith("/benchmark/") and "/trace" in request.url.path:
            trace_requests.append(request.url.path)
        return await call_next(request)

    output = tmp_path / "downloads"
    output.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        until(lambda: server.started)
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=url, trust_env=False) as client:
            thread_response = client.get(
                "/api/turns/trace/export", params={"session_id": session_id, "thread_id": session_id}
            )
            thread_response.raise_for_status()
            expected = [json.loads(line) for line in thread_response.iter_lines()]
        completed = subprocess.run(
            [
                shutil.which("node") or "node",
                str(ROOT / "tests/support/trace_download_browser_check.cjs"),
                url,
                str(output),
                session_id,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert trace_requests == [f"/benchmark/runs/{run_id}/tasks/{task_id}/trace/export"]
        project_file = output / f"thread-{session_id}-trace.jsonl"
        benchmark_file = output / f"benchmark-{run_id}-{task_id}-trace.jsonl"
        assert [json.loads(line) for line in project_file.read_text(encoding="utf-8").splitlines()] == expected
        assert [json.loads(line) for line in benchmark_file.read_text(encoding="utf-8").splitlines()] == events
        print(completed.stdout.strip())
        print(f"Browser download artifacts: {output}")
    finally:
        server.should_exit = True
        worker.join(timeout=15)
        listener.close()
        assert not worker.is_alive()


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    os.environ["PRAXIS_FRONTEND_DIST"] = str(ROOT / "frontend/dist")
    preview, *_ = export_preview_app(args.root)
    uvicorn.run(preview, host="127.0.0.1", port=args.port, log_level="warning")
