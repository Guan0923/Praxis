"""Definitions for workspace-confined filesystem tools."""

from __future__ import annotations

from ..base import Tool
from ..filesystem import WorkspaceFiles
from .schema import object_schema

_PATH_RULES = (
    "Use workspace:relative/path or project:relative/path. Approved absolute paths are also accepted. "
    "Bare paths use project when available, otherwise workspace; missing files never fall back to another root. "
)


def filesystem_read_tools(files: WorkspaceFiles) -> tuple[Tool, ...]:
    return (
        Tool(
            "read_file",
            (
                "Reads a bounded range from a UTF-8 text-file path inside an approved workspace or "
                "read-only Skill root, returning numbered normalized-LF lines and range metadata."
            ),
            files.read_file,
            object_schema(
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            _PATH_RULES + "A UTF-8 text file, or an absolute path in an approved read-only Skill root."
                        ),
                    },
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "The one-based line number at which reading starts. Defaults to 1.",
                    },
                    "start_column": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": (
                            "The one-based column within start_line at which reading starts. Defaults to 1."
                        ),
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 200,
                        "description": ("The maximum number of lines to return, from 1 to 1000. Defaults to 200."),
                    },
                },
                ["path"],
            ),
        ),
        Tool(
            "glob",
            (
                "Lists regular files under a selected directory whose relative paths match a case-sensitive glob "
                "pattern, returning sorted and bounded results."
            ),
            files.glob,
            object_schema(
                {
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_096,
                        "description": (
                            "The case-sensitive glob pattern to match against relative file paths. Use forward "
                            "slashes; *, ?, and character sets match within one path segment, while ** matches "
                            "across directories."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            _PATH_RULES + "The directory to search. When omitted, both "
                            "available workspaces are searched."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 200,
                        "description": (
                            "The maximum number of matching file paths to return, from 1 to 1000. Defaults to 200."
                        ),
                    },
                },
                ["pattern"],
            ),
        ),
        Tool(
            "grep",
            (
                "Searches UTF-8 text files line by line for literal text or a regular expression and returns each "
                "match as path:line:text."
            ),
            files.grep,
            object_schema(
                {
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The literal text or regular expression to find.",
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            _PATH_RULES + "The file or directory to search. When omitted, "
                            "both available workspaces are searched."
                        ),
                    },
                    "glob": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_096,
                        "default": "**/*",
                        "description": (
                            "The case-sensitive glob pattern used to select files below path. Defaults to **/*."
                        ),
                    },
                    "regex": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to interpret pattern as a regular expression. Defaults to false.",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether text matching is case-sensitive. Defaults to true.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 200,
                        "description": (
                            "The maximum number of matching lines to return, from 1 to 1000. Defaults to 200."
                        ),
                    },
                },
                ["pattern"],
            ),
        ),
    )


def filesystem_mutation_tools(files: WorkspaceFiles) -> tuple[Tool, ...]:
    return (
        Tool(
            "file_operation",
            (
                "Creates files or directories, writes UTF-8 files, or deletes files and directory trees. "
                "Create requires type=file or directory and never overwrites a file. Missing parent directories "
                "are created for file creation and whole-file writes. Write replaces the whole file only when "
                "start_line and end_line are both -1 and expected_content is empty; otherwise the inclusive "
                "line range must match expected_content exactly. Empty content clears the file or deletes the "
                "selected lines. Delete removes the target, including all contents of a directory."
            ),
            files.file_operation,
            _file_operation_schema(),
            requires_confirmation=True,
            read_only=False,
            workspace_confined=True,
        ),
    )


def _file_operation_schema() -> dict[str, object]:
    properties = {
        "operation": {
            "type": "string",
            "enum": ["create", "write", "delete"],
            "description": "Create a target, write a file, or delete a file or directory tree.",
        },
        "path": {
            "type": "string",
            "minLength": 1,
            "description": _PATH_RULES + "The target file or directory. Workspace roots cannot be deleted.",
        },
        "type": {
            "type": "string",
            "enum": ["file", "directory"],
            "description": "Required for create only: the kind of target to create.",
        },
        "content": {
            "type": "string",
            "description": (
                "Required text for write; optional initial text for create with type=file, defaulting to empty. "
                "An empty string clears the file or deletes selected lines; one newline inserts one blank line."
            ),
        },
        "start_line": {
            "type": "integer",
            "anyOf": [{"const": -1}, {"minimum": 1}],
            "default": -1,
            "description": "Write only: the one-based first line, inclusive, or -1 for a whole-file write.",
        },
        "end_line": {
            "type": "integer",
            "anyOf": [{"const": -1}, {"minimum": 1}],
            "default": -1,
            "description": "Write only: the one-based last line, inclusive, or -1 for a whole-file write.",
        },
        "expected_content": {
            "type": "string",
            "default": "",
            "description": (
                "Write only: exact selected line contents joined with newlines, without the last line ending. "
                "Defaults to empty. A mismatch is rejected; read the file again before editing."
            ),
        },
    }
    branches = []
    for operation, target_type, fields, required in (
        ("create", "file", ("type", "content"), ("type",)),
        ("create", "directory", ("type",), ("type",)),
        ("write", None, ("content", "start_line", "end_line", "expected_content"), ("content",)),
        ("delete", None, (), ()),
    ):
        branch_properties = {name: {} for name in ("operation", "path", *fields)}
        branch_properties["operation"] = {"const": operation}
        if target_type is not None:
            branch_properties["type"] = {"const": target_type}
        branches.append(object_schema(branch_properties, ["operation", "path", *required]))
    return {**object_schema(properties, ["operation", "path"]), "oneOf": branches}
