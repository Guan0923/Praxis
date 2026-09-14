from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState


@pytest.mark.parametrize(
    ("body", "content_type", "expected_error"),
    [
        (b"", "application/json", "空响应"),
        (b" \n\t", "application/json", "空响应"),
        (b"<!doctype html><html>private-upstream-page</html>", "text/html", "网页而不是 JSON"),
        (b"upstream unavailable", "text/plain", "无效 JSON"),
        (b'{"data":', "application/json", "无效 JSON"),
        (b'\xff{"data":[]}', "application/json", "无效 JSON"),
        (b'{"data":[{"id":"local-model"}]}', "application/json", None),
        (b'\xef\xbb\xbf{"data":[{"id":"local-model"}]}', "application/json", None),
    ],
)
def test_discover_models_reports_upstream_response_kind(tmp_path, body, content_type, expected_error):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with TestClient(create_app(WebAppState(tmp_path / "web"))) as client:
            response = client.post(
                "/api/settings/providers/models",
                json={
                    "base_url": f"http://127.0.0.1:{server.server_port}/api/paas/v4",
                },
            )
        assert paths == ["/api/paas/v4/models"]
        if expected_error:
            assert response.status_code == 502
            assert expected_error in response.json()["detail"]
            assert "HTTP 200" in response.json()["detail"]
            assert "JSONDecodeError" not in response.text
            assert "private-upstream-page" not in response.text
        else:
            assert response.status_code == 200
            assert response.json() == {"models": ["local-model"]}
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
