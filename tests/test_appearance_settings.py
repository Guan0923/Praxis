from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.storage.settings import LocalSettingsStore


def test_appearance_defaults_and_persists_without_changing_other_settings(tmp_path: Path) -> None:
    root = tmp_path / ".mini_agent"
    store = LocalSettingsStore(root / "runtime" / "state.db", root / "config.toml")
    assert store.appearance_config() == {"mode": "light"}
    store.update_profile(display_name="Appearance test", agent_preferences="Keep this preference")
    for mode in ("dark", "light", "dark"):
        store.update_appearance_config(mode)
        reopened = LocalSettingsStore(root / "runtime" / "state.db", root / "config.toml")
        assert reopened.appearance_config() == {"mode": mode}
    assert reopened.profile()["agent_preferences"] == "Keep this preference"
    with (root / "config.toml").open("rb") as source:
        assert tomllib.load(source)["appearance"] == {"mode": "dark"}


def test_appearance_api_validation_origin_protection_and_restart(tmp_path: Path) -> None:
    root = tmp_path / ".mini_agent"
    with TestClient(create_app(WebAppState(root))) as client:
        assert client.get("/api/settings").json()["appearance_config"] == {"mode": "light"}
        rejected = client.put(
            "/api/settings/appearance", json={"mode": "dark"}, headers={"Origin": "https://outside.example"}
        )
        assert rejected.status_code == 403
        assert (
            client.put(
                "/api/settings/appearance", json={"mode": "dark"}, headers={"Origin": "http://localhost:5173"}
            ).status_code
            == 400
        )
        for payload in ({"mode": "system"}, {"mode": True}, {}, {"mode": "dark", "profile": {}}):
            assert client.put("/api/settings/appearance", json=payload).status_code == 422
        assert client.put("/api/settings/appearance", json={"mode": "dark"}).json() == {"mode": "dark"}
        assert client.get("/api/settings").json()["appearance_config"] == {"mode": "dark"}
    with TestClient(create_app(WebAppState(root))) as reopened:
        assert reopened.get("/api/settings").json()["appearance_config"] == {"mode": "dark"}
