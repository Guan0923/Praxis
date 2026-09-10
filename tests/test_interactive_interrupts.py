from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from typing import Any

import pytest

from backend.api.chat import interrupts
from backend.runtime.core.contracts import InterruptRequest

DECISIONS = [
    ("plan", {"choice": "implement"}, "implement"),
    ("question", {"choice": "answer", "answers": {"storage": ["SQLite"]}}, "answer"),
    ("tool", {"choice": "allow_once"}, "continue"),
    ("resume", {"choice": "continue"}, "continue"),
    ("skill", {"choice": "trust"}, "trust"),
]


def test_interactive_decision_can_be_resolved_as_soon_as_it_is_published(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = interrupts.DecisionRegistry()
    monkeypatch.setattr(interrupts, "registry", registry)
    accepted: list[bool] = []

    def sink(event: dict[str, Any]) -> None:
        accepted.append(registry.resolve(event["data"]["decision_id"], {"choice": "allow_once"}))

    decide = interrupts.make_interactive_interrupt(sink)
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


def test_all_interactive_decisions_wait_until_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = interrupts.DecisionRegistry()
    monkeypatch.setattr(interrupts, "registry", registry)
    published: Queue[dict[str, Any]] = Queue()
    cancelled = Event()
    decide = interrupts.make_interactive_interrupt(published.put, cancel_requested=cancelled.is_set)
    # Set to 121 for the real-time regression check without slowing the default suite.
    wait_seconds = float(os.environ.get("PRAXIS_TEST_APPROVAL_WAIT_SECONDS", "0.2"))

    with ThreadPoolExecutor(max_workers=len(DECISIONS)) as executor:
        futures = {
            kind: executor.submit(decide, InterruptRequest(kind, "Waiting for user", {}))
            for kind, _response, _choice in DECISIONS
        }
        try:
            events = [published.get(timeout=5)["data"] for _ in DECISIONS]
            decision_ids = {event["kind"]: event["decision_id"] for event in events}
            cancelled.wait(wait_seconds)
            for kind, response, choice in DECISIONS:
                assert not futures[kind].done(), f"{kind} stopped waiting before the user answered"
                assert registry.resolve(decision_ids[kind], response)
                result = futures[kind].result(timeout=5)
                assert result.choice == choice
                assert result.answers == response.get("answers")
                assert not registry.resolve(decision_ids[kind], response)
        finally:
            cancelled.set()


@pytest.mark.parametrize(("kind", "response", "_choice"), DECISIONS)
def test_interactive_wait_can_be_stopped_and_discards_decision(
    monkeypatch: pytest.MonkeyPatch, kind: str, response: dict[str, Any], _choice: str
) -> None:
    registry = interrupts.DecisionRegistry()
    monkeypatch.setattr(interrupts, "registry", registry)
    published: Queue[dict[str, Any]] = Queue()
    cancelled = Event()
    decide = interrupts.make_interactive_interrupt(published.put, cancel_requested=cancelled.is_set)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(decide, InterruptRequest(kind, "Waiting for user", {}))
        try:
            decision_id = published.get(timeout=5)["data"]["decision_id"]
            cancelled.set()
            assert future.result(timeout=5).choice == "cancel"
            assert not registry.resolve(decision_id, response)
        finally:
            cancelled.set()


def test_cancellation_wins_when_decision_is_already_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = interrupts.DecisionRegistry()
    monkeypatch.setattr(interrupts, "registry", registry)
    cancelled = Event()

    def sink(event: dict[str, Any]) -> None:
        registry.resolve(event["data"]["decision_id"], {"choice": "implement"})
        cancelled.set()

    decide = interrupts.make_interactive_interrupt(sink, cancel_requested=cancelled.is_set)

    assert decide(InterruptRequest("plan", "Review plan", {})).choice == "cancel"
