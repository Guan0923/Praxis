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

from backend.configuration import ClientPaths
from backend.domain.runtime_state import NodeFrame, RuntimeState
from backend.providers import JsonHttpTransport
from backend.storage.sqlite import SQLiteSessionStore

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


def test_baseline_small_chunk_latency_and_outbox_size(tmp_path):
    baseline_transport = baseline_module("backend/src/providers/transport.py", "backend.providers").JsonHttpTransport
    baseline_events = baseline_module("backend/src/storage/sqlite_runtime/events.py", "backend.storage.sqlite_runtime")

    class BaselineStore(SQLiteSessionStore):
        _put_runtime_event = baseline_events.SQLiteRuntimeEventMixin._put_runtime_event

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
        for name, transport_type, store_type in [
            ("baseline", baseline_transport, BaselineStore),
            ("updated", JsonHttpTransport, SQLiteSessionStore),
        ]:
            transport = transport_type()
            started = perf_counter()
            events = transport.stream_json(f"http://127.0.0.1:{server.server_port}", {}, {}, 5)
            assert next(events) == {"delta": "small"}
            first_ms = (perf_counter() - started) * 1000
            list(events)
            transport.session.close()
            paths = ClientPaths(tmp_path / name)
            paths.ensure()
            store = store_type(paths)
            session = store.create_session("latency")
            parent = store.ensure_root_node(session.session_id)
            node = RuntimeState.create(
                session_id=session.session_id,
                thread_id=session.session_id,
                id="turn",
                parent=parent,
                user_content=[{"type": "text", "text": "history " * 8192}],
            )
            store.create_node_with_frame(node, NodeFrame.snapshot(node))
            updated = node.clone()
            updated.data[0][-1]["content"] = [{"type": "text", "text": "x", "status": "running"}]
            frame = NodeFrame.delta(node, updated, revision=1)
            started = perf_counter()
            store.update_node_with_frame(updated, frame)
            stored = store.runtime_event(node.session_id, frame.event_id)
            measurements[name] = {
                "first_small_event_ms": round(first_ms, 3),
                "outbox_bytes": len(json.dumps(stored, ensure_ascii=False, separators=(",", ":")).encode()),
                "delta_bytes": len(json.dumps(frame.to_dict(), ensure_ascii=False, separators=(",", ":")).encode()),
                "persist_and_read_ms": round((perf_counter() - started) * 1000, 3),
            }
        print("latency_baseline=" + json.dumps(measurements))
        assert measurements["updated"]["outbox_bytes"] < measurements["baseline"]["outbox_bytes"] / 10
        assert measurements["updated"]["first_small_event_ms"] < measurements["baseline"]["first_small_event_ms"] / 2
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
