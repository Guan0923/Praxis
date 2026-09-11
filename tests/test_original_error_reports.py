from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.error_handlers import error_response, install_error_handlers
from backend.domain import error_report, normalize_error_report, safe_error_message
from backend.domain.runtime_state import terminal_error_payload
from backend.sandbox import WindowsBrokerClient
from backend.sandbox.installation.results import read_result, result_path


def test_real_file_failure_survives_http_boundary(tmp_path):
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/failure")
    def failure():
        try:
            (tmp_path / "missing.txt").read_bytes()
        except OSError as exc:
            return error_response(exc, status_code=503)

    with TestClient(app) as client:
        response = client.get("/failure")
    report = response.json()["error_report"]
    assert response.status_code == 503
    assert report["type"] == "FileNotFoundError"
    assert report["errno"] == 2
    assert "in failure" in report["traceback"]
    assert "HTTPException" not in report["traceback"]


def test_real_sqlite_error_and_durable_payload():
    with sqlite3.connect(":memory:") as database:
        try:
            database.execute("SELECT * FROM nonexistent_table")
        except sqlite3.OperationalError as exc:
            report = error_report(exc)
    item = terminal_error_payload("server", report["message"], retryable=False, error_report=report)
    restored = json.loads(json.dumps(item))
    assert restored["error_report"]["type"] == "OperationalError"
    assert "nonexistent_table" in restored["error_report"]["message"]


def test_real_http_failure_redacts_url_and_retains_trace():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = requests.get(f"http://127.0.0.1:{server.server_port}/?access_token=private-test-value", timeout=3)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            report = error_report(exc)
        assert report["type"] == "HTTPError"
        assert "503" in report["message"]
        assert "private-test-value" not in json.dumps(report)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_report_preserves_remote_origin_without_rewrapping():
    remote = {"type": "OSError", "message": "invalid operation", "traceback": "remote.py:12", "errno": 22}
    error = RuntimeError("local receiver")
    error.error_report = remote
    assert error_report(error) == remote
    assert safe_error_message(error) == remote["message"]
    assert normalize_error_report({**remote, "locals": {"password": "hidden"}}) == remote


def test_trace_does_not_include_source_or_locals():
    hidden = "local-variable-must-not-leak"
    try:
        raise ValueError("test failure")
    except ValueError as exc:
        report = error_report(exc)
    assert hidden not in report["traceback"]
    assert "raise ValueError(" not in report["traceback"]


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_real_isolated_named_pipe_failure_is_not_wrapped():
    client = WindowsBrokerClient(pipe_name=rf"\\.\pipe\praxis-error-test-{uuid.uuid4().hex}")
    with pytest.raises(OSError) as failure:
        client._send(b"test")
    report = error_report(failure.value)
    assert report["type"] in {"FileNotFoundError", "OSError"}
    assert failure.value.__cause__ is None


@pytest.mark.skipif(os.name != "nt", reason="Protected Windows helper result")
def test_real_child_report_round_trip(tmp_path):
    directory = tmp_path / "Praxis" / "SandboxBroker"
    directory.mkdir(parents=True)
    request_id = uuid.uuid4().hex
    program = """
import sys
from pathlib import Path
from backend.sandbox.installation.results import write_result
try:
    Path(sys.argv[1], 'missing-child-file').read_bytes()
except OSError as error:
    write_result(Path(sys.argv[1]), sys.argv[2], error)
    raise SystemExit(6)
"""
    child = subprocess.run([sys.executable, "-c", program, str(directory), request_id], capture_output=True, timeout=10)
    report = read_result(directory, request_id)
    assert child.returncode == 6
    assert report["type"] == "FileNotFoundError"
    assert "missing-child-file" in report["message"]
    assert not result_path(directory, request_id).exists()
    assert read_result(directory, request_id) is None


def test_result_path_rejects_escape(tmp_path):
    with pytest.raises(ValueError):
        result_path(tmp_path / "Praxis" / "SandboxBroker", "../escape")
    with pytest.raises(ValueError):
        result_path(tmp_path, uuid.uuid4().hex)


def test_one_conversion_preserves_origin_and_secret_free_durable_turn(tmp_path):
    from backend.domain import TracePersistenceError
    from backend.domain.runtime_state import RuntimeState
    from tests.local_store import session_store

    def origin():
        raise OSError(22, "access_token=fixture-token")

    try:
        try:
            origin()
        except OSError as exc:
            raise TracePersistenceError("audit failed") from exc
    except TracePersistenceError as exc:
        report = error_report(exc)
    store = session_store(tmp_path)
    session = store.create_session("errors")
    root = store.ensure_root_node(session.session_id)
    turn = RuntimeState.create(
        session_id=session.session_id, thread_id=session.session_id, parent=root, user_content="test"
    )
    store.create_node(turn)
    turn.data[turn.current_data_idx].append(
        {
            "role": "assistant",
            "content": [terminal_error_payload("server", report["message"], retryable=False, error_report=report)],
        }
    )
    turn.status = "failed"
    store.finalize_node(turn)
    restored = session_store(tmp_path).get_node(session.session_id, turn.id)
    saved = restored.data[restored.current_data_idx][-1]["content"][0]["error_report"]
    assert saved == report
    assert saved["type"] == "OSError" and saved["errno"] == 22
    assert "in origin" in saved["traceback"]
    assert saved["traceback"].count("TracePersistenceError:") == 1
    assert "fixture-token" not in json.dumps(restored.to_dict())


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_real_broker_pipe_retains_server_origin_and_redacts_audit(tmp_path):
    import win32file
    import win32pipe

    from backend.sandbox.broker_service.configuration import BrokerConfiguration
    from backend.sandbox.broker_service.service import WindowsBrokerService

    pipe_name = rf"\\.\pipe\praxis-report-{uuid.uuid4().hex}"
    key = os.urandom(32)

    class Adapter:
        def reserve(self, _body):
            raise OSError(22, "Cookie: session=fixture-cookie; other=fixture-other")

    config = BrokerConfiguration.create(program_data=tmp_path, installation_id="test", pipe_name=pipe_name)
    service = WindowsBrokerService(config, adapter=Adapter(), is_windows=True)
    service._key = key
    pipe = win32pipe.CreateNamedPipe(
        pipe_name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
        1,
        1024 * 1024,
        1024 * 1024,
        3000,
        None,
    )
    failures = []

    def serve():
        try:
            win32pipe.ConnectNamedPipe(pipe, None)
            _, request = win32file.ReadFile(pipe, 1024 * 1024)
            win32file.WriteFile(pipe, service.handle(request))
            win32file.FlushFileBuffers(pipe)
        except Exception as exc:
            failures.append(exc)
        finally:
            pipe.Close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = WindowsBrokerClient(pipe_name=pipe_name, installation_key=key, is_windows=True)
    try:
        with pytest.raises(Exception) as received:
            client.request("reserve", {})
        report = error_report(received.value)
        assert report["type"] == "OSError" and report["errno"] == 22
        assert "in reserve" in report["traceback"]
        assert "in _send" not in report["traceback"]
        audit = config.audit_path.read_text()
        for secret in ("fixture-cookie", "fixture-other"):
            assert secret not in json.dumps(report) and secret not in audit
    finally:
        thread.join(5)
    assert not thread.is_alive() and not failures


def test_sse_error_report_is_not_collapsed_to_text():
    from backend.api.runtime_event_transport import _terminal_envelope

    report = {"type": "OSError", "message": "denied", "traceback": "server-origin", "winerror": 5}
    events = _terminal_envelope("turn", "failed", "denied", error_report=report)
    first = json.loads(events.splitlines()[0].removeprefix("data: "))
    assert first == {"type": "turn.error", "error_report": report}
    assert 'type="failed"' in events


def test_real_command_failure_keeps_exit_code_and_redacted_stderr(tmp_path):
    from backend.jobs import SubprocessJob
    from backend.jobs.output import CommandError
    from backend.tools.command import WorkspaceCommand

    job = SubprocessJob(
        "report-command",
        [sys.executable, "-c", "import sys; print('password=command-fixture-secret', file=sys.stderr); sys.exit(9)"],
        dict(os.environ),
        str(tmp_path),
        5,
    )
    job.start()
    assert job.wait(10)
    info = job.info()
    assert info.exit_code == 9
    assert info.error_report["type"] == "CommandError"
    assert "stderr:" in info.error_report["traceback"]
    assert "command-fixture-secret" not in json.dumps(info.error_report)
    with pytest.raises(CommandError) as failure:
        from backend.tools import ToolInvocationContext

        WorkspaceCommand(tmp_path)._read(job, ToolInvocationContext(), 0, 2000)
    assert failure.value is job.failure_exception
    assert failure.value.__cause__ is None


def test_real_child_exit_without_report_is_not_guessed(tmp_path, monkeypatch):
    from backend.sandbox.broker_service.installer import WindowsServiceInstaller
    from backend.sandbox.errors import BrokerInstallationError
    from tests.test_sandbox_broker_installer import _install_fake_pywin32

    child = subprocess.run([sys.executable, "-c", "raise SystemExit(17)"], capture_output=True, timeout=5)
    _install_fake_pywin32(monkeypatch, lambda **_kwargs: {"hProcess": object()}, exit_code=child.returncode)
    directory = tmp_path / "Praxis" / "SandboxBroker"
    directory.mkdir(parents=True)
    installer = WindowsServiceInstaller((sys.executable,), program_data_path=directory, is_windows=True)
    with pytest.raises(BrokerInstallationError) as failure:
        installer._run_elevated_transaction("repair", None)
    assert "17" in str(failure.value)
    assert "未取得子进程异常详情" in str(failure.value)
    assert failure.value.error_report is None
