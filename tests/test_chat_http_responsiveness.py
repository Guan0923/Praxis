"""Full loopback HTTP/Redis/browser verification with an unpaid local model."""

from __future__ import annotations

import json
import select
import shutil
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter, sleep, time
from uuid import uuid4

import pytest
import requests
import uvicorn

from backend.api.app import create_app
from backend.api.chat import routes as chat_routes
from backend.api.state import WebAppState
from backend.planning.llm import LLMPlanner
from backend.providers import LLMClient, ModelConfig
from backend.runtime import build_application
from backend.storage.message_queue import RedisMessageQueue


def test_browser_with_real_redis_http_and_small_model_chunks(tmp_path, monkeypatch, local_sandbox_runtime):
    root = Path(__file__).resolve().parents[1]
    if not (root / "frontend/dist/index.html").exists() or not shutil.which("node"):
        pytest.skip("Build frontend and install Node before the browser integration check")
    metrics = []
    release = threading.Event()

    class Model(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            metric = {"received": time() * 1000, "stream": bool(payload.get("stream"))}
            metrics.append(metric)
            self.send_response(200)
            if not payload.get("stream"):
                body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Local test"}}]}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            users = [m for m in payload["messages"] if m["role"] == "user"]
            prompt = str(users[-1]["content"]) if users else ""
            metric["prompt"] = prompt
            try:
                for token in ["LOCAL", " small", " stream", " response"]:
                    chunk = {"choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                    metric.setdefault("first_output", time() * 1000)
                    sleep(0.04)
                if "hold" in prompt:
                    deadline = perf_counter() + 30
                    while not release.is_set() and perf_counter() < deadline:
                        if select.select([self.connection], [], [], 0.02)[0] and not self.connection.recv(1):
                            metric["disconnected"] = time() * 1000
                            return
                self.wfile.write(
                    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                )
                self.wfile.flush()
                metric["finished"] = time() * 1000
            except (OSError, ConnectionError):
                metric["disconnected"] = time() * 1000
            finally:
                self.close_connection = True

    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    queue = RedisMessageQueue.from_url(key_prefix=f"mini-agent:test:chat-latency:{uuid4().hex}")
    queue.ping()

    class TestBroker:
        def status(self):
            return {"installed": True, "healthy": True, "code": "ready", "detail": "Explicit test launcher"}

    state = WebAppState(tmp_path / "data", message_queue=queue, sandbox_broker=TestBroker())
    config = ModelConfig("local-test", f"http://127.0.0.1:{model.server_port}/v1", "local-test")
    state.settings.update_provider_config({"base_url": config.base_url, "model": config.model})
    monkeypatch.setattr(state, "model_config", lambda *_args, **_kwargs: config)

    def local_application(_state, *, session_id, workspace=None, **_kwargs):
        metrics.append({"runtime_start": time() * 1000})
        application = build_application(
            workspace or state.session_workspace(session_id), planner_name="rule", paths=state.paths
        )
        tools = application.runner.tools
        application.runner.planner = LLMPlanner(LLMClient(config), tools.specs(), tools.read_only_specs())
        return application

    monkeypatch.setattr(chat_routes, "build_local_application", local_application)
    monkeypatch.setenv("MINI_AGENT_FRONTEND_DIST", str(root / "frontend/dist"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setenv("MINI_AGENT_ALLOWED_ORIGINS", f"http://127.0.0.1:{port}")
    app = create_app(state)

    @app.middleware("http")
    async def receive_timing(request, call_next):
        if request.method == "POST" and request.url.path == "/api/turns":
            metrics.append({"backend_received": time() * 1000})
        return await call_next(request)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    serving = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    serving.start()
    try:
        deadline = perf_counter() + 10
        while not server.started and perf_counter() < deadline:
            sleep(0.02)
        assert server.started
        url = f"http://127.0.0.1:{port}"
        assert requests.get(f"{url}/api/ready", timeout=3).status_code == 200
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests/support/chat_browser_check.cjs"), url, str(tmp_path)],
            cwd=root / "frontend",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=100,
        )
        print(result.stdout)
        assert result.returncode == 0, result.stderr[-6000:]
        stopped = [item for item in metrics if "disconnected" in item]
        assert len(stopped) >= 2, metrics
        verify_control_races(url, state, metrics)
        print("model_metrics=" + json.dumps(metrics))
    finally:
        release.set()
        server.should_exit = True
        serving.join(10)
        listener.close()
        model.shutdown()
        model.server_close()
        model_thread.join(3)
        keys = list(queue.client.scan_iter(f"{queue.key_prefix}:*"))
        if keys:
            queue.client.delete(*keys)
        queue.client.close()


def verify_control_races(url, state, metrics):
    for pause in [False, True]:
        sidebar = requests.post(f"{url}/api/sidebar-threads", json={}, timeout=5).json()
        session_id = sidebar["session_id"]
        thread_id = sidebar["thread_id"]
        turn_id = f"control-{uuid4().hex}"
        prompt = f"hold {turn_id}"
        body = {
            "id": turn_id,
            "session_id": session_id,
            "thread_id": thread_id,
            "parent_id": "",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            "permission_mode": "read_only",
            "running_mode": "agent",
        }
        assert requests.post(f"{url}/api/turns", json=body, timeout=5).status_code == 202
        deadline = perf_counter() + 8
        while (
            not any(item.get("prompt") == prompt and "first_output" in item for item in metrics)
            and perf_counter() < deadline
        ):
            sleep(0.02)
        assert any(item.get("prompt") == prompt for item in metrics)
        queue_url = f"{url}/api/sidebar-threads/{thread_id}/queued-messages"
        queued_ids = [str(uuid4()), str(uuid4())]
        for index in range(2):
            assert (
                requests.post(
                    queue_url,
                    json={"id": queued_ids[index], "content": f"finish instruction {index}", "references": []},
                    timeout=5,
                ).status_code
                == 201
            )
        controller = state.active_turn_cancellations[turn_id]
        if pause:
            with ThreadPoolExecutor(2) as pool:
                steer = pool.submit(
                    requests.post,
                    f"{url}/api/turns/{turn_id}/steer",
                    json={"delivery_id": "race", "message_ids": queued_ids},
                    timeout=5,
                )
                stop = pool.submit(requests.post, f"{url}/api/turns/{turn_id}/pause", timeout=5)
                assert stop.result().status_code == 200
                assert steer.result().status_code in {202, 409}
        else:
            # Keep both deliveries queued until one consumer can take them.
            with controller._lock:
                for index in range(2):
                    controller.dispatch_steering(
                        lambda index=index: state.message_queue.dispatch(
                            delivery_id=f"ordered-{index}",
                            message_ids=[queued_ids[index]],
                            session_id=session_id,
                            thread_id=thread_id,
                            turn_id=turn_id,
                        )
                    )
        deadline = perf_counter() + 10
        node = None
        while perf_counter() < deadline:
            nodes = requests.get(f"{url}/api/turns", params={"session_id": session_id}, timeout=5).json()
            node = next((item for item in nodes if item["id"] == turn_id), None)
            if node and node["status"] != "running" and turn_id not in state.active_turn_streams:
                break
            sleep(0.04)
        assert node and node["status"] == ("paused" if pause else "success"), node
        messages = node["data"][node["current_data_idx"]]
        applied = [item.get("delivery_id") for item in messages if item["role"] == "user"]
        remaining = requests.get(queue_url, timeout=5).json()
        assert all(item["state"] == "pending" for item in remaining)
        if not pause:
            assert applied[-2:] == ["ordered-0", "ordered-1"] and len(set(applied)) == len(applied)
            assert not remaining
        elif "race" not in applied:
            assert {item["id"] for item in remaining} == set(queued_ids)
        assert any(item.get("prompt") == prompt and "disconnected" in item for item in metrics)
