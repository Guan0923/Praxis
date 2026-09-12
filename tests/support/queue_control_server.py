"""Manual browser fixture: real HTTP/SSE with an unpaid loopback model."""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import sleep

import uvicorn

from backend.api.app import create_app
from backend.api.chat import routes as chat_routes
from backend.api.state import WebAppState
from backend.planning.llm import LLMPlanner
from backend.providers import LLMClient, ModelConfig
from backend.runtime import build_application
from backend.runtime.application import factory
from tests.testing_sandbox import DirectTestSandboxLauncher


def main():
    port = int(sys.argv[1])
    data = Path(sys.argv[2]).resolve()
    release = threading.Event()
    calls = []

    class Model(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            if not body.get("stream"):
                payload = json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": "Local fixture"}}]}
                ).encode()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            prompt = next(str(item["content"]) for item in reversed(body["messages"]) if item["role"] == "user")
            calls.append({"prompt": prompt, "closed": False})
            call = calls[-1]
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                index = 0
                while index < 4 or ("hold" in prompt and not release.is_set()):
                    token = f"part{index:04d} "
                    chunk = {"choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                    index += 1
                    sleep(0.05)
                self.wfile.write(
                    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                )
                self.wfile.flush()
            except OSError:
                call["interrupted"] = True
            finally:
                call["closed"] = True
                self.close_connection = True

    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    worker = threading.Thread(target=model.serve_forever, daemon=True)
    worker.start()

    class Broker:
        def status(self):
            return {"installed": True, "healthy": True, "code": "ready", "detail": "Local fixture"}

    state = WebAppState(data, sandbox_broker=Broker())
    config = ModelConfig("local-test", f"http://127.0.0.1:{model.server_port}/v1", "local-test")
    state.settings.update_provider_config({"base_url": config.base_url, "model": config.model})
    state.model_config = lambda *_args, **_kwargs: config
    factory._sandbox_runtime = lambda *_args, **_kwargs: (DirectTestSandboxLauncher(), {})

    def application(_state, *, session_id, workspace=None, **_kwargs):
        app = build_application(
            workspace or state.session_workspace(session_id),
            planner_name="rule",
            paths=state.paths,
            todo_store=state.todo_store,
        )
        tools = app.runner.tools
        app.runner.planner = LLMPlanner(LLMClient(config), tools.specs(), tools.read_only_specs())
        return app

    chat_routes.build_local_application = application
    os.environ["PRAXIS_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{port}"
    app = create_app(state)

    @app.get("/fixture/status")
    def status():
        return {"calls": calls}

    @app.post("/fixture/release")
    def finish(reset: bool = False):
        if reset:
            release.clear()
        else:
            release.set()
        return {"released": not reset}

    @app.post("/fixture/shutdown")
    def shutdown():
        release.set()
        server.should_exit = True
        return {"stopping": True}

    # Fixture controls must precede the application's static-file mount.
    app.router.routes[:] = app.router.routes[-3:] + app.router.routes[:-3]

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    try:
        server.run()
    finally:
        release.set()
        model.shutdown()
        model.server_close()
        worker.join(3)


if __name__ == "__main__":
    main()
