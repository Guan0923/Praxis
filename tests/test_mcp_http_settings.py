from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.domain import safe_error_message
from backend.mcp import settings as mcp_settings
from backend.mcp.client.adapters import _parameters
from backend.mcp.config import read_server_configs
from backend.mcp.json_config import STORED_SECRET, McpJsonDocument


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


@pytest.fixture
def setup_store(tmp_path, monkeypatch):
    keyring = MemoryKeyring()
    monkeypatch.setattr(mcp_settings, "_keyring_module", lambda: keyring)
    monkeypatch.setattr("keyring.get_password", keyring.get_password)
    state = WebAppState(tmp_path / "state")
    return state, keyring


def test_json_roundtrip_protects_secrets_and_preserves_defaults(setup_store):
    state, keyring = setup_store
    document = {
        "mcpServers": {
            "remote": {
                "type": "streamableHttp",
                "url": "https://example.test/mcp?a=%2f&tavilyApiKey=test-url-secret",
                "headers": {"Authorization": "test-header-secret", "X-Region": "local"},
            },
            "local": {"type": "stdio", "command": "node", "env": {"TOKEN": "test-env-secret"}},
            "events": {"type": "sse", "url": "http://127.0.0.1:1234/sse", "timeout": 50, "disabled": True},
        }
    }
    with TestClient(create_app(state)) as client:
        saved = client.put("/api/settings/mcp", json=document)
        assert saved.status_code == 200, saved.text
        raw = state.paths.mcp_file.read_text(encoding="utf-8")
        for secret in ("test-url-secret", "test-header-secret", "test-env-secret"):
            assert secret not in raw + saved.text
        public = saved.json()["mcpServers"]
        assert public["remote"]["headers"]["authorization"] == STORED_SECRET
        assert public["local"]["timeout"] == 30 and public["local"]["args"] == []
        assert public["events"]["timeout"] == 50 and public["events"]["disabled"]
        servers = {server.name: server for server in read_server_configs(state.paths.mcp_file)}
        assert _parameters(servers["local"]).cwd == str(Path.home())
        original_credentials = dict(keyring.values)
        assert client.put("/api/settings/mcp", json={"mcpServers": public}).status_code == 200
        assert keyring.values == original_credentials
        public["remote"]["headers"]["authorization"] = "replacement-secret"
        del public["local"]
        replaced = client.put("/api/settings/mcp", json={"mcpServers": public})
        assert replaced.status_code == 200 and "replacement-secret" not in replaced.text
        assert "test-env-secret" not in keyring.values.values()
        assert "replacement-secret" in keyring.values.values()
        assert client.put("/api/settings/mcp", json={"mcpServers": {}}).status_code == 200
        assert not keyring.values and not read_server_configs(state.paths.mcp_file)


@pytest.mark.parametrize(
    "server",
    [
        {"url": "https://example.test/mcp"},
        {"type": "stdio"},
        {"type": "sse"},
        {"type": "streamableHttp", "url": "ftp://example.test/mcp"},
        {"type": "stdio", "command": "node", "url": "https://example.test/mcp"},
        {"type": "sse", "url": "https://example.test/mcp", "command": "node"},
        {"type": "stdio", "command": "node", "timeout": 0},
        {"type": "stdio", "command": "node", "timeout": True},
        {"type": "stdio", "command": "node", "disabled": "false"},
        {"type": "stdio", "command": "node", "args": "server.js"},
        {"type": "streamableHttp", "url": "https://example.test/mcp", "headers": {"Host": "other"}},
        {"type": "streamableHttp", "url": "https://example.test/mcp", "headers": {"X-A": "one", "x-a": "two"}},
        {
            "type": "streamableHttp",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "secret-value\r\ninjected"},
        },
    ],
)
def test_invalid_document_does_not_mutate_or_echo_values(setup_store, server):
    state, _ = setup_store
    with TestClient(create_app(state)) as client:
        response = client.put("/api/settings/mcp", json={"mcpServers": {"invalid": server}})
        assert response.status_code == 422
        assert "secret-value" not in response.text and '"input"' not in response.text
        assert client.get("/api/settings/mcp").json()["mcpServers"] == {}


def test_secret_placeholders_cannot_reference_another_server(setup_store):
    state, _ = setup_store
    with TestClient(create_app(state)) as client:
        response = client.put(
            "/api/settings/mcp",
            json={
                "mcpServers": {
                    "new": {"type": "sse", "url": "https://example.test", "headers": {"Authorization": STORED_SECRET}}
                }
            },
        )
        assert response.status_code == 422


def test_failed_write_rolls_back_all_credentials(setup_store, monkeypatch):
    state, keyring = setup_store
    store = mcp_settings.McpSettingsStore(state.paths)

    def document(value):
        return McpJsonDocument.model_validate(
            {
                "mcpServers": {
                    "remote": {"type": "sse", "url": "https://example.test", "headers": {"Authorization": value}}
                }
            }
        )

    store.replace_document(document("original-secret"))
    original = state.paths.mcp_file.read_text(encoding="utf-8")

    def fail_write(*args):
        raise OSError("write failed")

    monkeypatch.setattr(mcp_settings, "atomic_write_text", fail_write)
    with pytest.raises(ValueError, match="Unable to save"):
        store.replace_document(document("replacement-secret"))
    assert list(keyring.values.values()) == ["original-secret"]
    assert state.paths.mcp_file.read_text(encoding="utf-8") == original


def test_query_credentials_are_redacted_from_error_reports():
    message = safe_error_message(ValueError("Failed https://example.test/mcp?tavilyApiKey=secret-value&x=1"))
    assert "secret-value" not in message and "x=1" in message


@pytest.mark.parametrize("transport", ["sse", "streamableHttp"])
def test_real_http_json_save_initialize_and_call(setup_store, transport):
    from backend.mcp.client import start_external_tools
    from tests.test_mcp_capabilities import peer

    state, _ = setup_store
    query = "tavilyApiKey=test-query-secret&escaped=%2f%2B&repeat=1&repeat=2" if transport == "streamableHttp" else None
    with peer(transport=transport, token="raw-authorization-token", query=query) as endpoint:
        url = endpoint.url + ("?" + query if query else "")
        with TestClient(create_app(state)) as client:
            saved = client.put(
                "/api/settings/mcp",
                json={
                    "mcpServers": {
                        "fixture": {
                            "type": transport,
                            "url": url,
                            "headers": {"Authorization": "raw-authorization-token"},
                            "timeout": 5,
                            "disabled": True,
                        }
                    }
                },
            )
            assert saved.status_code == 200
            tested = client.post("/api/settings/mcp/servers/fixture/test")
            assert tested.status_code == 200, tested.text
            assert tested.json()["counts"]["tools"] == 3
            assert client.get("/api/settings/mcp").json()["mcpServers"]["fixture"]["disabled"]
        from dataclasses import replace

        config = replace(read_server_configs(state.paths.mcp_file)[0], enabled=True)
        resources = start_external_tools((config,))
        try:
            result = resources.manager.call("fixture", "echo", {"value": "json-settings"})
            assert "json-settings" in result
        finally:
            resources.close()


def test_real_connection_failure_keeps_cause_and_redacts_url(setup_store, caplog):
    from tests.test_mcp_capabilities import peer

    state, _ = setup_store
    with peer(transport="http", token="required-token") as endpoint:
        with TestClient(create_app(state), raise_server_exceptions=False) as client:
            response = client.put(
                "/api/settings/mcp",
                json={
                    "mcpServers": {
                        "bad": {
                            "type": "streamableHttp",
                            "url": endpoint.url + "?tavilyApiKey=test-private-query",
                        }
                    }
                },
            )
            assert response.status_code == 200
            failed = client.post("/api/settings/mcp/servers/bad/test")
            assert failed.status_code == 500
            assert "401" in failed.text
            assert "test-private-query" not in failed.text + caplog.text
            assert "all configured servers failed" not in failed.text
