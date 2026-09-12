"""Real local HTTP fixture. No model calls, production data, or broker mutations."""

import argparse
import json
from pathlib import Path

import uvicorn
from test_agent_threads import _agent_create
from test_view_state_sync import seed

from backend.api.app import create_app
from backend.api.state import WebAppState


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8018)
    args = parser.parse_args()
    state = WebAppState(args.data_root)
    manifest = args.data_root / "navigation-fixture.json"
    if not manifest.exists():
        records = []
        for number in range(7):
            session, turn = seed(state.session_store, 7 if number == 0 else 5)
            state.session_store.create_sidebar_thread(
                session_id=session.session_id, thread_id=session.session_id, title=f"Navigation {number + 1}"
            )
            state.session_store.rename_session(session.session_id, f"Navigation {number + 1}")
            if number == 0:
                turn.data[0][-1]["content"] = [
                    {
                        "type": "text",
                        "text": "\n\n".join(
                            f"Paragraph {index}: navigation performance test with a long message."
                            for index in range(200)
                        ),
                        "status": "success",
                    }
                ]
                state.session_store.update_node(turn)
            records.append({"session": session.session_id, "thread": session.session_id, "turn": turn.id})
        manifest.write_text(json.dumps(records), encoding="utf-8")
    records = json.loads(manifest.read_text(encoding="utf-8"))
    if "agent" not in records[0]:
        first = records[0]
        parent = state.session_store.get_node(first["session"], first["turn"])
        child = _agent_create(first["session"], parent, name="navigation-worker")
        child.turn.provider_name = "local"
        state.session_store.create_agent_thread(first["session"], child)
        child.turn.data[0][-1]["content"] = [
            {"type": "text", "text": "Child conversation content", "status": "success"}
        ]
        child.turn.status = "success"
        state.session_store.finalize_node(child.turn)
        first["agent"] = child.node.thread_id
        manifest.write_text(json.dumps(records), encoding="utf-8")
    child_turn = state.session_store.load_turn_page(records[0]["session"], records[0]["agent"]).turns[-1]
    if not child_turn.provider_name:
        child_turn.provider_name = "local"
        state.session_store.update_node(child_turn)
    target = state.session_store.get_node(records[0]["session"], records[0]["turn"])
    if target.data[0][-1]["content"][0]["type"] != "reasoning":
        for version in target.data:
            version[-1]["content"].insert(
                0, {"type": "reasoning", "text": "Saved expansion fixture", "status": "success"}
            )
        state.session_store.update_node(target)
    uvicorn.run(create_app(state), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
