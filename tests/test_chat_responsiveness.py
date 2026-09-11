"""Local socket/process tests for prompt streaming and operation interruption."""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter as monotonic
from time import sleep

import pytest

from backend.api.pause_control import TurnPauseController
from backend.domain import AssistantMessage, ToolMessage
from backend.mcp.client import start_external_tools
from backend.mcp.config import McpServerConfig
from backend.planning import RuleBasedPlanner
from backend.providers import JsonHttpTransport, ModelTransportError
from backend.runtime import AgentRunner
from backend.runtime.execution.tool_batch import ToolBatchExecutor
from backend.tools import Tool, ToolError, ToolInvocationContext, ToolRegistry
from backend.tools.command import WorkspaceCommand
from tests.testing_sandbox import DirectTestSandboxLauncher


@pytest.mark.parametrize("phase", ["headers", "stream", "json"])
def test_pause_interrupts_a_real_silent_model_connection(phase: str) -> None:
    ready = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    controller = TurnPauseController()
    failures: list[Exception] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if phase != "headers":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if phase == "stream" else "application/json")
                self.send_header("Content-Length", "10000")
                self.end_headers()
                self.wfile.flush()
            ready.set()
            release.wait(5)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    transport = JsonHttpTransport()

    def request():
        try:
            call = transport.post_json if phase == "json" else transport.stream_json
            result = call(
                f"http://127.0.0.1:{server.server_port}/",
                {},
                {},
                10,
                cancel_requested=controller.is_requested,
                register_abort=controller.register_abort,
            )
            if phase != "json":
                list(result)
        except Exception as exc:
            failures.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    try:
        assert ready.wait(3)
        started = monotonic()
        controller.request_pause()
        assert monotonic() - started < 0.5, "pause itself must not block on the reader"
        assert finished.wait(1), "the provider must stop without the server sending another byte"
        print(f"pause_{phase}_ms={(monotonic() - started) * 1000:.1f}")
        assert len(failures) == 1 and isinstance(failures[0], ModelTransportError)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(3)
        serving.join(3)
        transport.session.close()


def test_small_model_event_is_visible_before_the_response_finishes() -> None:
    release = threading.Event()
    received = threading.Event()
    failures: list[Exception] = []
    events: list[dict] = []
    payload = b'data: {"delta":"first"}\n\ndata: [DONE]\n\n'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload.split(b"data: [DONE]")[0])
            self.wfile.flush()
            release.wait(3)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    transport = JsonHttpTransport()

    def consume():
        try:
            for event in transport.stream_json(f"http://127.0.0.1:{server.server_port}/", {}, {}, 5):
                events.append(event)
                received.set()
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=consume, daemon=True)
    started = monotonic()
    worker.start()
    try:
        assert received.wait(1), "small SSE events must not wait for a full read buffer"
        print(f"first_small_event_ms={(monotonic() - started) * 1000:.1f}")
    finally:
        release.set()
        worker.join(3)
        server.shutdown()
        server.server_close()
        serving.join(3)
        transport.session.close()
    assert events == [{"delta": "first"}] and not failures


@pytest.mark.parametrize("action", ["pause", "steer"])
def test_parallel_real_commands_stop_before_their_natural_completion(tmp_path: Path, action: str) -> None:
    controller = TurnPauseController()
    command = WorkspaceCommand(tmp_path, terminal_type="cmd" if sys.platform == "win32" else "bash")
    registry = ToolRegistry(
        [Tool("run_command", "test command", command.run, context_handler=command.run_with_context)]
    )
    launcher = DirectTestSandboxLauncher()
    # Use the real command job's built-in process-tree termination.
    launcher.terminate_tree = None
    runtime = AgentRunner(
        RuleBasedPlanner(),
        registry,
        max_tool_parellel=2,
        workspace_root=str(tmp_path),
        sandbox_launcher=launcher,
    ).new_runtime(task="interrupt commands")
    runtime.services.suspend_requested = controller.is_requested
    runtime.services.operation_interrupted = controller.operation_interrupted
    runtime.services.register_operation_abort = controller.register_abort
    inbox: list[str] = []

    def take():
        messages = inbox[:]
        inbox.clear()
        return messages

    runtime.services.steering = lambda: controller.take_steering(take)
    message = AssistantMessage(
        tool_messages=[
            ToolMessage(
                name="run_command",
                call_id=f"command-{index}",
                arguments={
                    "cmd": f"{sys.executable} {Path(__file__).parent / 'support' / 'slow_command.py'} {index}",
                    "yield_time_ms": 45000,
                },
            )
            for index in range(3)
        ]
    )
    runtime.state.active_message = message
    results = []
    worker = threading.Thread(target=lambda: results.append(ToolBatchExecutor().execute(runtime, message)), daemon=True)
    worker.start()
    deadline = monotonic() + 8
    try:
        while len(list(tmp_path.glob("started-*"))) < 2 and monotonic() < deadline:
            sleep(0.02)
        assert len(list(tmp_path.glob("started-*"))) == 2, [item.content for item in message.tool_messages]
        started = monotonic()
        if action == "pause":
            controller.request_pause()
        else:
            controller.dispatch_steering(lambda: inbox.append("new instruction"))
        worker.join(3)
        assert not worker.is_alive(), "real command jobs must be terminated, not merely hidden"
        print(f"parallel_command_{action}_ms={(monotonic() - started) * 1000:.1f}")
        assert not list(tmp_path.glob("late-*"))
        assert len(list(tmp_path.glob("started-*"))) == 2
        assert all(not result.success and not result.retryable for result in results[0].outcomes)
        assert (results[0].steering is not None) == (action == "steer")
    finally:
        controller.request_pause()
        worker.join(5)


def test_real_mcp_call_is_cancelled_without_closing_the_server(tmp_path: Path) -> None:
    script = Path(__file__).parent / "support" / "cancellable_mcp_server.py"
    resources = start_external_tools((McpServerConfig("cancel", sys.executable, (str(script),)),))
    controller = TurnPauseController()
    context = ToolInvocationContext(
        cancel_requested=controller.operation_interrupted, register_abort=controller.register_abort
    )
    failures: list[Exception] = []

    def invoke():
        try:
            resources[0].context_handler(context, directory=str(tmp_path))
        except ToolError as exc:
            failures.append(exc)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        deadline = monotonic() + 5
        while not (tmp_path / "started").exists() and monotonic() < deadline:
            sleep(0.02)
        assert (tmp_path / "started").exists()
        started = monotonic()
        controller.request_steering()
        worker.join(2)
        assert not worker.is_alive() and failures
        deadline = monotonic() + 2
        while not (tmp_path / "cancelled").exists() and monotonic() < deadline:
            sleep(0.02)
        assert (tmp_path / "cancelled").exists(), "the server must receive cancellation"
        print(f"mcp_steer_ms={(monotonic() - started) * 1000:.1f}")
        assert resources[1].handler() == "alive"
    finally:
        controller.request_pause()
        resources.close()
        worker.join(3)
