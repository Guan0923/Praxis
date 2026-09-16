"""Plan-only request_plan_review control protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from backend.domain import ToolSpec
from backend.domain.file_paths import ScopedPaths
from backend.tools.filesystem import WorkspaceFiles

REQUEST_PLAN_REVIEW_NAME = "request_plan_review"

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["plan", "plan_name"],
    "properties": {
        "plan_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "description": "Plan filename without directories. Saved to workspace/plan/<plan_name>.md; an existing file is overwritten. The .md suffix is optional.",
        },
        "plan": {
            "type": "string",
            "minLength": 1,
            "description": "The complete implementation plan in Markdown.",
        },
    },
}

REQUEST_PLAN_REVIEW_SPEC = ToolSpec(
    name=REQUEST_PLAN_REVIEW_NAME,
    description=(
        "Pauses the current Plan-mode run to present a complete implementation plan for user review and returns "
        "the user's decision. Saves the Markdown plan under workspace/plan/, creating missing directories "
        "and overwriting the same filename before review. The tool result is the saved file content; "
        "implementation uses the latest saved file approved by the user."
    ),
    parameters=_PARAMETERS,
)

_VALIDATOR = Draft202012Validator(_PARAMETERS)


def parse_plan_review(arguments: dict[str, Any]) -> str:
    """Validate a model-generated Plan Review request and return its plan."""

    if not isinstance(arguments, dict):
        raise ValueError("request_plan_review arguments must be an object.")
    error = next(_VALIDATOR.iter_errors(arguments), None)
    if error is not None:
        raise ValueError(f"Invalid request_plan_review arguments at {error.json_path}: {error.message}")
    name = arguments["plan_name"].strip()
    if (
        not name
        or name in {".", ".."}
        or name.endswith((".", " "))
        or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in name)
    ):
        raise ValueError("plan_name must be a filename without directories or reserved characters.")
    if name.split(".", 1)[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise ValueError("plan_name must not be a reserved device name.")
    plan = arguments["plan"].strip()
    if not plan:
        raise ValueError("request_plan_review plan must not be blank.")
    return plan


def save_plan_review(arguments: dict[str, Any], workspace_root: str | None) -> str:
    """Persist the reviewed text before publishing a successful submission."""
    plan = parse_plan_review(arguments)
    if not workspace_root:
        raise ValueError("Plan review requires a workspace directory.")
    WorkspaceFiles(Path(workspace_root)).write_file(f"workspace:{plan_review_path(arguments)}", plan, overwrite=True)
    return read_plan_review(arguments, workspace_root)


def plan_review_path(arguments: dict[str, Any]) -> str:
    parse_plan_review(arguments)
    name = arguments["plan_name"].strip()
    return f"plan/{name}" if name.lower().endswith(".md") else f"plan/{name}.md"


def read_plan_review(arguments: dict[str, Any], workspace_root: str | None) -> str:
    if not workspace_root:
        raise ValueError("Plan review requires a workspace directory.")
    target = ScopedPaths(Path(workspace_root)).resolve(f"workspace:{plan_review_path(arguments)}")
    content = target.read_text(encoding="utf-8-sig")
    if not content.strip():
        raise ValueError("The plan file must not be blank.")
    return content


def approved_plan(content: str) -> str:
    return f"<approved_plan>\n{content}\n</approved_plan>"
