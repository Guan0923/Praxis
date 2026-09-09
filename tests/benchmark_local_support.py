"""Loopback-only model and fixtures for benchmark transport/runtime acceptance."""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import sleep

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.providers import ModelConfig
from backend.storage.message_queue import MemoryMessageQueue
from benchmarks.grading.programmatic import final_answer_contains, tool_used
from benchmarks.model import BenchmarkTask, Seed, SeedFile, SourceMetadata


def local_tasks() -> list[BenchmarkTask]:
    return [
        BenchmarkTask(
            name=f"local-check-{index}",
            capability="terminal",
            difficulty="easy",
            description="Local HTTP and real file-tool acceptance task.",
            prompt=f"BENCHMARK_LOCAL_CHECK {index}. Read notes/alpha.md and answer LOCAL_FILE_OK.",
            source=SourceMetadata(
                "Local acceptance", str(index), "http://127.0.0.1", "test-only", "MIT", "Not a scored public benchmark."
            ),
            seed=Seed(files=(SeedFile("notes/alpha.md", "LOCAL_FILE_OK\n"),)),
            checkers=(
                final_answer_contains("LOCAL_FILE_OK" if index != 2 else "EXPECTED_DIFFERENT_ANSWER"),
                tool_used("read_file"),
            ),
        )
        for index in range(1, 10)
    ]


@contextmanager
def local_model(*, delay: float = 0, tool_name: str = "read_file", tool_arguments: dict | None = None):
    calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"model": payload["model"], "stream": payload.get("stream", False)})
            has_tool = any(message.get("role") == "tool" for message in payload["messages"])
            if has_tool:
                message = {"role": "assistant", "content": "LOCAL_FILE_OK"}
                finish = "stop"
            else:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "read-local",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(
                                    tool_arguments if tool_arguments is not None else {"path": "notes/alpha.md"}
                                ),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            sleep(delay)
            if payload.get("stream"):
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
                chunks = [
                    {
                        "id": "local-response",
                        "object": "chat.completion.chunk",
                        "model": payload["model"],
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    },
                    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
                ]
                body = ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps(
                    {
                        "id": "local-response",
                        "model": payload["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": finish,
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    }
                ).encode()
                content_type = "application/json"
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, OSError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = ModelConfig(
        "local-dummy-value", f"http://127.0.0.1:{server.server_port}/v1", "local-check", max_tokens=256
    )
    try:
        yield config, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def acceptance_app(root: Path, config: ModelConfig):
    web = WebAppState(root, message_queue=MemoryMessageQueue())
    web.model_config = lambda *_args, **_kwargs: config
    return create_app(web)


if __name__ == "__main__":
    import argparse
    import os

    import uvicorn

    import benchmarks.tasks

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=2)
    parser.add_argument(
        "--public", action="store_true", help="Show the public suite with a free local smoke-test model."
    )
    args = parser.parse_args()
    os.environ["PRAXIS_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{args.port}"
    if not args.public:
        benchmarks.tasks.ALL_TASKS = local_tasks()
        benchmarks.tasks.TASKS_BY_NAME = {task.name: task for task in benchmarks.tasks.ALL_TASKS}
    kwargs = {"tool_name": "container_exec", "tool_arguments": {"command": "pwd"}} if args.public else {}
    with local_model(delay=args.delay, **kwargs) as (config, _calls):
        uvicorn.run(acceptance_app(args.root, config), host="127.0.0.1", port=args.port, log_level="warning")
