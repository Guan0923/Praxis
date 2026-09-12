from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

import pytest

from backend.sandbox.control.broker import WindowsBrokerClient
from backend.sandbox.control.pipe_connection import connect_pipe
from backend.sandbox.control.status_request import status_request

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")


def create_pipe(name: str):
    import win32pipe

    return win32pipe.CreateNamedPipe(
        name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE,
        win32pipe.PIPE_UNLIMITED_INSTANCES,
        65536,
        65536,
        0,
        None,
    )


@pytest.mark.parametrize("kind", ["command", "status"])
def test_concurrent_requests_wait_for_busy_pipe_without_replay(monkeypatch, kind):
    import win32file
    import win32pipe

    name = rf"\\.\pipe\praxis-connection-test-{uuid.uuid4().hex}"
    occupied = create_pipe(name)
    owner = connect_pipe(name, deadline=time.monotonic() + 1)
    waiting = Event()
    received = []
    errors = []
    native_wait = win32pipe.WaitNamedPipe

    def wait_named_pipe(*args):
        waiting.set()
        return native_wait(*args)

    monkeypatch.setattr(win32pipe, "WaitNamedPipe", wait_named_pipe)

    def server():
        try:
            if not waiting.wait(5):
                raise TimeoutError("Client did not wait for a free pipe")
            for _ in range(8):
                pipe = create_pipe(name)
                try:
                    win32pipe.ConnectNamedPipe(pipe, None)
                    payload = bytes(win32file.ReadFile(pipe, 4096)[1])
                    received.append(payload)
                    win32file.WriteFile(pipe, payload)
                    win32file.FlushFileBuffers(pipe)
                finally:
                    pipe.Close()
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=server, daemon=True)
    thread.start()
    payloads = [f"request-{index}".encode() for index in range(8)]

    def send(payload):
        if kind == "status":
            return status_request(name, payload, 3)
        return WindowsBrokerClient(pipe_name=name)._send(payload)

    try:
        with ThreadPoolExecutor(max_workers=8) as clients:
            assert list(clients.map(send, payloads)) == payloads
        thread.join(5)
        assert not thread.is_alive() and not errors
        assert sorted(received) == payloads
    finally:
        owner.Close()
        occupied.Close()


def test_busy_connection_has_a_deadline():
    name = rf"\\.\pipe\praxis-connection-test-{uuid.uuid4().hex}"
    pipe = create_pipe(name)
    owner = connect_pipe(name, deadline=time.monotonic() + 1)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="connection timed out"):
            connect_pipe(name, deadline=started + 0.05)
        assert time.monotonic() - started < 1
    finally:
        owner.Close()
        pipe.Close()


def test_write_failure_is_not_replayed(monkeypatch):
    import pywintypes
    import win32file

    from backend.sandbox.control import pipe_connection

    calls = []
    failure = pywintypes.error(109, "WriteFile", "Test pipe closed")

    class Pipe:
        def Close(self):
            calls.append("close")

    def connect(*args, **kwargs):
        calls.append("connect")
        return Pipe()

    def write(*args):
        calls.append("write")
        raise failure

    monkeypatch.setattr(pipe_connection, "connect_pipe", connect)
    monkeypatch.setattr(win32file, "WriteFile", write)
    with pytest.raises(pywintypes.error) as caught:
        WindowsBrokerClient()._send(b"command")
    assert caught.value is failure
    assert calls == ["connect", "write", "close"]
