"""Compare the pinned pre-change commit with the working changes using local I/O."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter, sleep
from types import ModuleType

import pytest

from backend.providers import JsonHttpTransport

BASELINE_REF = "dbf161ec97d90b99ab67a3a1eedf0320ccda1f45"


def baseline_module(path: str, package: str) -> ModuleType:
    root = Path(__file__).resolve().parents[1]
    try:
        source = subprocess.check_output(
            ["git", "show", f"{BASELINE_REF}:{path}"], cwd=root, encoding="utf-8", stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError:
        pytest.skip("The pre-change commit is unavailable in this checkout")
    module = ModuleType("latency_baseline")
    module.__package__ = package
    exec(compile(source, f"{BASELINE_REF}:{path}", "exec"), module.__dict__)
    return module


def test_baseline_small_chunk_latency():
    baseline_transport = baseline_module("backend/src/providers/transport.py", "backend.providers").JsonHttpTransport

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            first = b'data: {"delta":"small"}\n\n'
            last = b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(first + last)))
            self.end_headers()
            self.wfile.write(first)
            self.wfile.flush()
            sleep(0.4)
            self.wfile.write(last)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    measurements = {}
    try:
        for name, transport_type in [
            ("baseline", baseline_transport),
            ("updated", JsonHttpTransport),
        ]:
            transport = transport_type()
            started = perf_counter()
            events = transport.stream_json(f"http://127.0.0.1:{server.server_port}", {}, {}, 5)
            assert next(events) == {"delta": "small"}
            first_ms = (perf_counter() - started) * 1000
            list(events)
            transport.session.close()
            measurements[name] = {"first_small_event_ms": round(first_ms, 3)}
        print("latency_baseline=" + json.dumps(measurements))
        assert measurements["updated"]["first_small_event_ms"] < measurements["baseline"]["first_small_event_ms"] / 2
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
