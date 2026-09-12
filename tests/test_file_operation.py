"""Real filesystem and runtime coverage for the unified mutation tool."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from backend.domain import AssistantMessage, ToolMessage
from backend.providers import ChatCompletionsAdapter, MessagesAdapter, ModelConfig, ResponsesAdapter
from backend.runtime import AgentRunner
from backend.runtime.core.contracts import InterruptDecision
from backend.tools import ConfirmationRequired, ToolError, ToolRegistry, WorkspaceFiles, build_tool_registry


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    return build_tool_registry(tmp_path)


def invoke(registry: ToolRegistry, operation: str, path: str, **arguments: object) -> str:
    return registry.invoke("file_operation", {"operation": operation, "path": path, **arguments}, confirmed=True)


def test_create_and_write_make_missing_parents_and_preserve_create_protection(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    invoke(registry, "create", "new/empty.txt", type="file")
    assert (tmp_path / "new/empty.txt").read_bytes() == b""
    invoke(registry, "create", "new/initial.txt", type="file", content="original")
    with pytest.raises(ToolError, match="already exists"):
        invoke(registry, "create", "new/initial.txt", type="file", content="replacement")
    assert (tmp_path / "new/initial.txt").read_text() == "original"

    invoke(registry, "create", "folders/nested", type="directory")
    invoke(registry, "create", "folders/nested", type="directory")
    assert (tmp_path / "folders/nested").is_dir()
    for path, target_type in (("new/initial.txt", "directory"), ("folders/nested", "file")):
        with pytest.raises(ToolError):
            invoke(registry, "create", path, type=target_type)

    invoke(registry, "write", "other/parents/file.txt", content="first")
    invoke(registry, "write", "other/parents/file.txt", content="second")
    assert (tmp_path / "other/parents/file.txt").read_text() == "second"
    invoke(registry, "write", "other/parents/file.txt", content="", start_line=-1, end_line=-1, expected_content="")
    assert (tmp_path / "other/parents/file.txt").read_bytes() == b""
    invoke(registry, "write", "empty/new.txt", content="")
    assert (tmp_path / "empty/new.txt").is_file()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"operation": "mkdir"},
        {"operation": "create"},
        {"operation": "create", "type": "unknown"},
        {"operation": "create", "type": "directory", "content": ""},
        {"operation": "create", "type": "file", "start_line": -1},
        {"operation": "create", "type": "file", "expected_content": ""},
        {"operation": "create", "type": "file", "content": None},
        {"operation": "write"},
        {"operation": "write", "content": None},
        {"operation": "write", "content": ["text"]},
        {"operation": "write", "content": "", "type": "file"},
        {"operation": "write", "content": "", "overwrite": True},
        {"operation": "write", "content": "", "start_line": 0},
        {"operation": "write", "content": "", "end_line": -2},
        {"operation": "write", "content": "", "start_line": True},
        {"operation": "write", "content": "", "expected_content": []},
        {"operation": "delete", "content": ""},
        {"operation": "delete", "type": "file"},
        {"operation": "delete", "end_line": -1},
        {"operation": "delete", "recursive": True},
        {"operation": "delete", "path": ""},
    ],
)
def test_invalid_arguments_never_create_parents(
    tmp_path: Path, registry: ToolRegistry, arguments: dict[str, object]
) -> None:
    with pytest.raises(ToolError, match="Invalid arguments"):
        registry.invoke("file_operation", {"path": "untouched/target.txt", **arguments}, confirmed=True)
    assert not (tmp_path / "untouched").exists()


@pytest.mark.parametrize("old_name", ["create_directory", "write_file", "edit_file"])
def test_old_tool_names_are_unregistered(registry: ToolRegistry, old_name: str) -> None:
    with pytest.raises(ToolError, match="Unknown tool"):
        registry.invoke(old_name, {}, confirmed=True)


@pytest.mark.parametrize(
    ("original", "start", "end", "expected", "content", "result"),
    [
        (b"one\r\ntwo\r\nthree", 1, 2, "one\ntwo", "first\nsecond", b"first\r\nsecond\r\nthree"),
        (b"one\r\ntwo\r\nthree", 1, 2, "one\r\ntwo", "first\r\nsecond", b"first\r\nsecond\r\nthree"),
        (b"one\ntwo\nthree", 2, 2, "two", "", b"one\nthree"),
        (b"one\ntwo\nthree", 2, 2, "two", "\n", b"one\n\nthree"),
        (b"one\n\nthree", 2, 2, "", "blank replaced", b"one\nblank replaced\nthree"),
        (b"one\n\n\nthree", 2, 3, "\n", "", b"one\nthree"),
        (b"one\n two \nthree", 2, 2, " two ", " new ", b"one\n new \nthree"),
        (b"one\ntwo", 2, 2, "two", "last", b"one\nlast"),
        (b"one\ntwo\n", 2, 2, "two", "last", b"one\nlast\n"),
    ],
)
def test_line_writes_keep_exact_range_and_existing_newline_rules(
    tmp_path: Path,
    registry: ToolRegistry,
    original: bytes,
    start: int,
    end: int,
    expected: str,
    content: str,
    result: bytes,
) -> None:
    path = tmp_path / "lines.txt"
    path.write_bytes(original)
    invoke(registry, "write", "lines.txt", start_line=start, end_line=end, expected_content=expected, content=content)
    assert path.read_bytes() == result


@pytest.mark.parametrize(
    "range_arguments",
    [
        {"start_line": 1},
        {"end_line": 1},
        {"expected_content": "one"},
        {"start_line": 2, "end_line": 1, "expected_content": "two"},
        {"start_line": 4, "end_line": 4, "expected_content": "four"},
        {"start_line": 1, "end_line": 2, "expected_content": "one"},
        {"start_line": 2, "end_line": 2, "expected_content": "one"},
        {"start_line": 2, "end_line": 2, "expected_content": " two "},
        {"start_line": 2, "end_line": 2, "expected_content": ""},
    ],
)
def test_invalid_or_stale_range_never_falls_back_to_overwrite_or_search(
    tmp_path: Path, registry: ToolRegistry, range_arguments: dict[str, object]
) -> None:
    path = tmp_path / "lines.txt"
    original = b"one\ntwo\nthree"
    path.write_bytes(original)
    with pytest.raises(ToolError):
        invoke(registry, "write", "lines.txt", content="replacement", **range_arguments)
    assert path.read_bytes() == original


def test_line_write_does_not_create_missing_file(tmp_path: Path, registry: ToolRegistry) -> None:
    with pytest.raises(ToolError, match="Not a file"):
        invoke(registry, "write", "missing/file.txt", start_line=1, end_line=1, expected_content="", content="new")
    assert not (tmp_path / "missing").exists()


def test_delete_file_and_directory_tree_keeps_parent(tmp_path: Path, registry: ToolRegistry) -> None:
    invoke(registry, "create", "parent/tree/nested/file.txt", type="file", content="remove")
    invoke(registry, "create", "parent/tree/empty", type="directory")
    invoke(registry, "delete", "parent/tree")
    assert not (tmp_path / "parent/tree").exists()
    assert (tmp_path / "parent").is_dir()
    invoke(registry, "create", "parent/file.txt", type="file")
    invoke(registry, "delete", "parent/file.txt")
    assert not (tmp_path / "parent/file.txt").exists()
    invoke(registry, "delete", "parent")
    with pytest.raises(ToolError, match="Not a file or directory"):
        invoke(registry, "delete", "missing")


def test_delete_rejects_roots_and_ancestors_before_removing_anything(tmp_path: Path) -> None:
    project = tmp_path / "parent/project"
    project.mkdir(parents=True)
    marker = tmp_path / "parent/keep.txt"
    marker.write_text("keep", encoding="utf-8")
    registry = build_tool_registry(tmp_path, project_workspace=project)
    for path in ("workspace:", "project:", str(tmp_path), str(project), "workspace:parent"):
        with pytest.raises(ToolError):
            invoke(registry, "delete", path)
    assert marker.read_text() == "keep"
    assert project.is_dir()


@pytest.mark.parametrize(
    "operation,arguments", [("create", {"type": "file"}), ("write", {"content": "new"}), ("delete", {})]
)
def test_mutations_cannot_reach_outside_workspace_or_read_only_skill(
    tmp_path: Path, operation: str, arguments: dict[str, object]
) -> None:
    workspace = tmp_path / "workspace"
    skill = tmp_path / "skill"
    workspace.mkdir()
    skill.mkdir()
    marker = skill / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    registry = build_tool_registry(workspace, workspace_files=WorkspaceFiles(workspace, read_file_roots=(skill,)))
    for path in (str(marker), "../skill/keep.txt", str(skill / "missing/new.txt")):
        with pytest.raises(ToolError):
            invoke(registry, operation, path, **arguments)
    assert marker.read_text() == "keep"
    assert not (skill / "missing").exists()


def test_recursive_delete_rejects_nested_links_before_deleting_regular_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    tree = workspace / "tree"
    tree.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside", encoding="utf-8")
    (tree / "keep.txt").write_text("inside", encoding="utf-8")
    link = tree / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links is not permitted on this system.")
    with pytest.raises(ToolError, match="links|reparse"):
        invoke(build_tool_registry(workspace), "delete", "tree")
    assert (tree / "keep.txt").read_text() == "inside"
    assert (outside / "keep.txt").read_text() == "outside"


@pytest.mark.skipif(os.name != "nt", reason="Windows enforces deletion protection on read-only files.")
def test_delete_reports_real_windows_access_denial(tmp_path: Path, registry: ToolRegistry) -> None:
    path = tmp_path / "protected.txt"
    path.write_text("keep", encoding="utf-8")
    path.chmod(stat.S_IREAD)
    try:
        with pytest.raises(ToolError):
            invoke(registry, "delete", "protected.txt")
        assert path.read_text() == "keep"
    finally:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)


class _FileOperationPlanner:
    def __init__(self, arguments: dict[str, object]) -> None:
        self.arguments = arguments
        self.calls = 0
        self.feedback: list[ToolMessage] = []

    def decide(self, runtime) -> AssistantMessage:
        self.calls += 1
        if self.calls == 1:
            return AssistantMessage(
                tool_messages=[ToolMessage(name="file_operation", call_id="file_call", arguments=self.arguments)]
            )
        self.feedback = list(runtime.state.messages[-1].tool_messages)
        return AssistantMessage(content="The tool result was received.")


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "create", "path": "new/folder", "type": "directory"},
        {"operation": "create", "path": "new/file.txt", "type": "file"},
        {"operation": "write", "path": "new/file.txt", "content": "new"},
        {"operation": "write", "path": "existing/file.txt", "content": ""},
        {
            "operation": "write",
            "path": "existing/file.txt",
            "content": "new",
            "start_line": 1,
            "end_line": 1,
            "expected_content": "keep",
        },
        {"operation": "delete", "path": "existing/file.txt"},
        {"operation": "delete", "path": "existing"},
    ],
)
def test_denied_real_operations_have_no_filesystem_side_effects(
    tmp_path: Path, registry: ToolRegistry, arguments: dict[str, object]
) -> None:
    path = tmp_path / "existing/file.txt"
    path.parent.mkdir()
    path.write_text("keep", encoding="utf-8")
    with pytest.raises(ConfirmationRequired):
        registry.invoke("file_operation", arguments)

    planner = _FileOperationPlanner(arguments)
    with AgentRunner(planner, registry, workspace_root=str(tmp_path)) as runner:
        runtime = runner.new_runtime(task="file operation", interrupt=lambda _request: InterruptDecision("deny"))
        runtime.state.permission_mode = "read_only"
        result = runner.run(runtime)
    assert result.status == "completed"
    assert planner.feedback[0].failure_code == "user_denied"
    assert path.read_text() == "keep"
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize(
    "arguments,message",
    [
        (
            {
                "operation": "write",
                "path": "file.txt",
                "start_line": 1,
                "end_line": 1,
                "expected_content": "stale",
                "content": "new",
            },
            "Read the file again",
        ),
        ({"operation": "write", "path": "file.txt", "start_line": 1, "content": "new"}, "end_line"),
        ({"operation": "write", "path": "file.txt"}, "Invalid arguments"),
        ({"operation": "delete", "path": "absent"}, "Not a file or directory"),
    ],
)
def test_real_runtime_returns_failed_tool_result_to_planner(
    tmp_path: Path, registry: ToolRegistry, arguments: dict[str, object], message: str
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("keep", encoding="utf-8")
    planner = _FileOperationPlanner(arguments)
    events = []
    with AgentRunner(planner, registry, workspace_root=str(tmp_path)) as runner:
        runtime = runner.new_runtime(task="file operation", on_event=events.append)
        runtime.state.permission_mode = "workspace_write"
        runtime.services.runtime_node_event = events.append
        result = runner.run(runtime)
        runtime.state.model = "test-model"
        runtime.state.request_parameters = {"max_tokens": 128}
        runtime.exchange.messages = list(runtime.state.messages)
        config = ModelConfig("unused-test-key", "https://example.test/v1", "test-model")
        chat = ChatCompletionsAdapter(config).prepare_request(runtime)
        responses = ResponsesAdapter(config).prepare_request(runtime)
        messages = MessagesAdapter(config).prepare_request(runtime)
    assert result.status == "completed"
    assert planner.calls == 2
    feedback = planner.feedback[0]
    assert feedback.name == "file_operation"
    assert feedback.status == "failed"
    assert message in feedback.content
    assert result.retries == 0
    assert any(event.kind == "tool_failed" for event in events)
    assert [item["content"] for item in chat["messages"] if item["role"] == "tool"] == [feedback.content]
    assert [item["output"] for item in responses["input"] if item.get("type") == "function_call_output"] == [
        feedback.content
    ]
    message_results = [
        block
        for item in messages["messages"]
        if item["role"] == "user"
        for block in item["content"]
        if block["type"] == "tool_result"
    ]
    assert [(item["content"], item["is_error"]) for item in message_results] == [(feedback.content, True)]
    assert path.read_text() == "keep"
