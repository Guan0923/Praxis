from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.runtime import build_application
from backend.runtime.application import factory
from backend.tools import ToolError


def _write_skill(root: Path, directory: str, *, name: str = "demo") -> Path:
    skill = root / directory
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Demo settings skill.\n"
        "metadata:\n"
        "  owner: local\n"
        "allowed-tools:\n"
        "  - read_file\n"
        "---\n"
        "Use the demo workflow.\n",
        encoding="utf-8",
    )
    return skill


def test_skill_settings_persist_and_apply_to_the_next_runner(
    tmp_path: Path,
    local_sandbox_runtime: None,
) -> None:
    root = tmp_path / "user-data"
    state = WebAppState(root)
    skill = _write_skill(state.paths.skills_dir, "folder-id", name="folder-id")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    current = build_application(workspace, planner_name="rule", paths=state.paths)
    try:
        with TestClient(create_app(state)) as client:
            payload = client.get("/api/settings/skills").json()
            assert payload["enabled"] is True
            assert payload["skills"] == [
                {
                    "directory": "folder-id",
                    "name": "folder-id",
                    "description": "Demo settings skill.",
                    "metadata": {"owner": "local"},
                    "allowed_tools": ["read_file"],
                    "root": skill.resolve().as_posix(),
                    "enabled": True,
                }
            ]
            assert client.put("/api/settings/skills/folder-id/enabled", json={"enabled": "false"}).status_code == 422
            assert client.put("/api/settings/skills/folder-id/enabled", json={"enabled": False}).status_code == 200
            assert client.put("/api/settings/skills/enabled", json={"enabled": "false"}).status_code == 422

        manifest = skill / "SKILL.md"
        assert current.runner.skill_catalog.names() == ("folder-id",)
        assert "Use the demo workflow." in current.runner.tools.invoke("read_file", {"path": str(manifest)})

        next_runner = build_application(workspace, planner_name="rule", paths=ClientPaths(root))
        try:
            assert next_runner.runner.skill_catalog.names() == ()
            with pytest.raises(ToolError, match="approved workspace"):
                next_runner.runner.tools.invoke("read_file", {"path": str(manifest)})
        finally:
            next_runner.close()
    finally:
        current.close()

    assert state.settings.skill_config() == {"disabled": ["folder-id"]}


def test_skill_import_cancel_validation_copy_conflict_and_delete(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "imports", "demo")
    nested = source / "references" / "guide.md"
    nested.parent.mkdir()
    nested.write_text("guide", encoding="utf-8")
    root = tmp_path / "user-data"
    state = WebAppState(root, project_picker=lambda: None)
    with TestClient(create_app(state)) as client:
        assert client.post("/api/settings/skills/import").status_code == 204

    state = WebAppState(root, project_picker=lambda: source)
    with TestClient(create_app(state)) as client:
        imported = client.post("/api/settings/skills/import")
        assert imported.status_code == 201
        assert imported.json() == {"directory": "demo"}
        assert (state.paths.skills_dir / "demo" / "references" / "guide.md").read_text(encoding="utf-8") == "guide"
        assert client.post("/api/settings/skills/import").status_code == 409
        assert client.delete("/api/settings/skills/%2E%2E").status_code == 404
        assert client.delete("/api/settings/skills/demo").status_code == 204
        assert not (state.paths.skills_dir / "demo").exists()

    invalid = tmp_path / "invalid-skill"
    invalid.mkdir()
    state = WebAppState(tmp_path / "invalid-user-data", project_picker=lambda: invalid)
    with TestClient(create_app(state)) as client:
        assert client.post("/api/settings/skills/import").status_code == 422


def test_mcp_master_switch_skips_parsing_and_real_connection_test_closes(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "user-data")
    paths.ensure()
    paths.mcp_file.write_text("not valid toml", encoding="utf-8")

    assert len(factory._external_resources(paths, {})) == 0
    with pytest.raises(ToolError, match="Invalid MCP configuration"):
        factory._external_resources(paths, {"capabilities": {"mcp": True}})

    state = WebAppState(tmp_path / "api-user-data")
    script = Path(__file__).parent / "support" / "trace_mcp_server.py"
    payload = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "cwd": str(script.parent),
        "env": {},
        "disabled": True,
    }
    with TestClient(create_app(state)) as client:
        assert client.put("/api/settings/mcp", json={"mcpServers": {"trace": payload}}).status_code == 200
        tested = client.post("/api/settings/mcp/servers/trace/test")
        assert tested.status_code == 200, tested.text
        assert tested.json()["tools"] == ["mcp_trace_inspect_trace"]
        assert tested.json()["protocol_version"] == "2026-07-28"
        assert tested.json()["counts"] == {"tools": 1, "resources": 0, "resource_templates": 0, "prompts": 0}
