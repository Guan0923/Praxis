"""Responses summary segments through HTTP, runtime events, and persisted Turns."""

from __future__ import annotations

from backend.domain.runtime_state.deltas import apply_turn_delta
from backend.tools import ToolRegistry
from tests import test_incomplete_responses as harness
from tests.local_store import session_store


def test_summary_segments_replace_title_and_preserve_body(tmp_path, monkeypatch):
    first = "**Inspecting** the files."
    second = "**Checking** the result."
    corrected = "**Verified** the result."

    def events(_protocol, response):
        def summary(kind, index, value):
            return {
                "type": f"response.reasoning_summary_text.{kind}",
                "item_id": "reasoning-1",
                "output_index": 0,
                "summary_index": index,
                "delta" if kind == "delta" else "text": value,
            }

        return [
            summary("delta", 0, "**Inspecting**"),
            summary("delta", 0, " the files."),
            summary("done", 0, first),
            summary("delta", 1, "**Checking**"),
            summary("delta", 1, " the result."),
            summary("done", 1, corrected),
            {"type": "response.output_text.delta", "delta": "done"},
            {"type": "response.completed", "response": response},
        ]

    monkeypatch.setattr(harness, "stream_events", events)
    response = harness.wire_response("responses", text="done", reasoning=first + "\n\n" + corrected)
    observed = []
    with harness.local_provider("responses", [response]) as (url, received):
        with harness.canonical_runner(tmp_path, "responses", url, ToolRegistry()) as bound:
            runner, runtime, bridge, _store, frames, runtime_events = bound
            result = runner.run(runtime)
            terminal = bridge.finish("success")
            assert result.status == "completed" and len(received) == 1
            for frame in frames:
                if frame.type == "turn.snapshot":
                    replay = frame.turn.to_dict()
                else:
                    apply_turn_delta(replay, frame.to_dict())
                items = replay["data"][0][-1]["content"]
                observed.extend(item["summary"] for item in items if item.get("type") == "reasoning")
            reasoning = next(item for item in terminal.assistant_items if item["type"] == "reasoning")
            assert (reasoning["summary"], reasoning["text"]) == (corrected, first + "\n\n" + corrected)
            assert second in observed and corrected in observed
            assert replay == terminal.to_dict()
            assert len([event for event in runtime_events if event.kind == "thinking_summary"]) == 5
            reopened = session_store(tmp_path / "data")
            assert reopened.get_node(runtime.state.session_id, terminal.id).to_dict() == terminal.to_dict()
