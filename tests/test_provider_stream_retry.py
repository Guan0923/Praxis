import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from backend.domain import UserMessage
from backend.planning import RuleBasedPlanner
from backend.providers import LLMClient, ModelConfig
from backend.providers.errors import ModelResponseError
from backend.runtime import AgentRunner
from backend.tools import ToolRegistry


@pytest.fixture
def stream_server():
    state = SimpleNamespace(errors=[], requests=[])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state.requests.append(json.loads(body))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [{"choices": [{"index": 0, "delta": {"content": "partial"}}]}]
            if state.errors:
                events.append({"error": state.errors.pop(0)})
            else:
                events = [{"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]}]
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def request_runtime(events):
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry(), max_transport_retries=5).new_runtime(task="hello")
    runtime.exchange.messages = [UserMessage(content="hello")]
    runtime.exchange.stream = True
    runtime.services.publish = events.append
    return runtime


@pytest.mark.parametrize("field", ["code", "type"])
@pytest.mark.parametrize(
    "code", ["upstream_stream_read_error", "upstream_http2_stream_error", "upstream_stream_truncated"]
)
def test_connection_interruption_classification(field, code):
    for diagnostics in (
        {"error": {field: code}},
        {"provider_error": {field: code}},
        {"provider_error": {"error": {field: code}}},
    ):
        assert ModelResponseError("failure", diagnostics=diagnostics).is_connection_interruption


@pytest.mark.parametrize("recover", [True, False])
def test_stream_interruption_retries_three_times_over_local_http(stream_server, monkeypatch, recover):
    stream_server.errors = [
        {"type": "upstream_error", "code": "upstream_stream_read_error", "message": "interrupted"}
        for _ in range(3 if recover else 4)
    ]
    events = []
    runtime = request_runtime(events)
    chunks = []
    runtime.exchange.on_content = chunks.append
    client = LLMClient(ModelConfig("local-test", stream_server.url, "demo"))
    monkeypatch.setattr("backend.providers.client.time", SimpleNamespace(sleep=lambda _delay: None))

    if recover:
        response = client.run(runtime)
        assert response.message.content == "done"
    else:
        with pytest.raises(ModelResponseError, match="interrupted"):
            client.run(runtime)

    assert len(stream_server.requests) == 4
    assert all(body == stream_server.requests[0] for body in stream_server.requests)
    retries = [event for event in events if event.kind == "model_retry"]
    assert [event.data["attempt"] for event in retries] == [1, 2, 3]
    assert all(event.data["max_transport_retries"] == 3 for event in retries)
    assert len([event for event in events if event.kind == "model_error"]) == (0 if recover else 1)
    assert events[-1].kind == ("model_response" if recover else "model_error")
    assert chunks == (["partial"] * 3 + ["done"] if recover else ["partial"] * 4)


@pytest.mark.parametrize("code", [None, "invalid_request_error", "upstream_error", {"unexpected": "shape"}])
def test_message_alone_does_not_retry(stream_server, code):
    stream_server.errors = [
        {"code": code, "type": "upstream_error", "message": "Upstream response stream was interrupted"}
    ]
    events = []
    client = LLMClient(ModelConfig("local-test", stream_server.url, "demo"))
    with pytest.raises(ModelResponseError):
        client.run(request_runtime(events))
    assert len(stream_server.requests) == 1
    assert [event.kind for event in events] == ["model_request", "model_error"]


def test_pause_during_stream_retry_does_not_send_again(stream_server):
    stream_server.errors = [{"type": "upstream_stream_truncated", "message": "interrupted"}]
    events = []
    runtime = request_runtime(events)
    paused = threading.Event()
    runtime.services.suspend_requested = paused.is_set

    def register_abort(callback):
        paused.set()
        callback()
        return lambda: None

    def publish(event):
        events.append(event)
        if event.kind == "model_retry":
            runtime.services.register_operation_abort = register_abort

    runtime.services.publish = publish
    client = LLMClient(ModelConfig("local-test", stream_server.url, "demo"))
    with pytest.raises(ModelResponseError):
        client.run(runtime)
    assert len(stream_server.requests) == 1
    assert [event.kind for event in events] == ["model_request", "model_retry"]
