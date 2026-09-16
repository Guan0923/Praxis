from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from backend.tools import SafeWebFetcher
from tests.test_web import FakeResponse, FakeSession, public_resolver


@pytest.mark.parametrize("declared_length", [True, False])
def test_fetch_accepts_large_html_with_or_without_content_length(declared_length: bool) -> None:
    body = b"<script>" + b"x" * 2_000_001 + b"</script><p>Content after the former limit.</p>"
    headers = {"Content-Type": "text/html"}
    if declared_length:
        headers["Content-Length"] = str(len(body))
    response = FakeResponse(200, headers, body)
    fetcher = SafeWebFetcher(session=FakeSession([response]), resolver=public_resolver)

    output = fetcher.fetch("https://example.com/large", max_chars=100)

    assert "Content after the former limit." in output
    assert response.closed


def test_read_large_response_from_real_local_http_server() -> None:
    body = b"x" * 2_000_001 + b"end"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(f"http://127.0.0.1:{server.server_port}/", stream=True, timeout=(5, 15)) as response:
                assert SafeWebFetcher()._read_body(response) == body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
