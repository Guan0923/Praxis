from __future__ import annotations

from typing import Any

import pytest

from backend.api.chat import interrupts
from backend.runtime.core.contracts import InterruptRequest


def test_interactive_decision_can_be_resolved_as_soon_as_it_is_published(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = interrupts.DecisionRegistry()
    monkeypatch.setattr(interrupts, "registry", registry)
    accepted: list[bool] = []

    def sink(event: dict[str, Any]) -> None:
        accepted.append(registry.resolve(event["data"]["decision_id"], {"choice": "allow_once"}))

    decide = interrupts.make_interactive_interrupt(sink, timeout=0.01)
    result = decide(InterruptRequest("tool", "Approve tool?", {"tool": "read_mcp_resource"}))

    assert accepted == [True]
    assert result.choice == "continue"


def test_interactive_decision_is_discarded_when_publishing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = interrupts.DecisionRegistry()
    monkeypatch.setattr(interrupts, "registry", registry)
    decision_ids: list[str] = []

    def sink(event: dict[str, Any]) -> None:
        decision_ids.append(event["data"]["decision_id"])
        raise RuntimeError("event delivery failed")

    decide = interrupts.make_interactive_interrupt(sink)
    with pytest.raises(RuntimeError, match="event delivery failed"):
        decide(InterruptRequest("tool", "Approve tool?", {"tool": "read_mcp_resource"}))

    assert registry.resolve(decision_ids[0], {"choice": "allow_once"}) is False
