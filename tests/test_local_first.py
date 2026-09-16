from __future__ import annotations

from pathlib import Path

from backend.configuration import ClientPaths, initialize_config, load_config
from backend.runtime import RuntimeState
from backend.storage.sqlite import SQLiteSessionStore


def test_fresh_home_uses_exact_five_item_layout_and_ignores_legacy_env(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = workspace / ".env"
    legacy.write_text("API_KEY=secret\nBASE_URL=https://model.test\nMODEL=demo\n", encoding="utf-8")
    home = tmp_path / "home"
    paths = ClientPaths.from_home(home)

    assert paths.root == home / ".praxis"

    initialize_config(paths, workspace)

    assert legacy.exists()
    assert {item.name for item in paths.root.iterdir()} == {"mcp", "plugins", "runtime", "skills", "config.toml"}
    assert {item.name for item in paths.runtime_dir.iterdir()} == {"state.db", "projects.db"}
    assert "model" not in load_config(paths.config_file)
    assert not (paths.root / "sync").exists()
    assert not (paths.root / "user.db").exists()
    assert not (paths.root / "projects.db").exists()
    assert not (home / ".praxis-cache").exists()


def test_sqlite_persists_empty_runtime_and_time_zone_locally(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "praxis"))
    session = store.create_session("empty")
    state = RuntimeState(session_id=session.session_id, timezone="UTC")

    store.save_runtime(state)

    assert store.load_runtime(session.session_id).timezone == "UTC"
