"""Packaged system-prompt loading and composition."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

from backend.domain import PlanningError

_PROMPT_NAMES = ("instruction", "default", "plan", "title")


class PromptConfigurationError(PlanningError):
    """Raised when packaged prompt resources cannot form a valid system prompt."""


def _read_prompt(name: str) -> str:
    if name not in _PROMPT_NAMES:
        raise PromptConfigurationError(f"Unknown prompt resource: {name!r}.")
    resource = files(__package__).joinpath(f"{name}.md")
    try:
        content = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise PromptConfigurationError(f"Prompt resource is unavailable: {name}.md") from exc
    if not content.strip():
        raise PromptConfigurationError(f"Prompt resource must not be empty: {name}.md")
    return content.strip()


@lru_cache(maxsize=1)
def compose_system_prompt() -> str:
    """Load the mode-independent system instructions."""

    return _read_prompt("instruction")


@lru_cache(maxsize=2)
def collaboration_mode_prompt(mode: str) -> str:
    """Keep the complete selected mode instructions in chronological context."""

    if mode not in {"agent", "plan"}:
        raise PromptConfigurationError(f"Unsupported prompt mode: {mode!r}.")
    content = _read_prompt("default" if mode == "agent" else "plan")
    return f"<collaboration_mode>\n{content}\n</collaboration_mode>"


@lru_cache(maxsize=1)
def load_title_prompt() -> str:
    """Return the standalone conversation-title system prompt."""

    return _read_prompt("title")
