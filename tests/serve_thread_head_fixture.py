"""Isolated real HTTP/browser fixture; never calls a model or changes Broker settings."""

import argparse
import json
import os
from pathlib import Path

import uvicorn

from backend.api.app import create_app
from backend.api.runtime_event_transport import publish_frame, publish_terminal
from backend.api.state import WebAppState
from backend.domain.runtime_state import NodeFrame, RuntimeState
from tests.test_agent_threads import _agent_create
from tests.test_view_state_sync import seed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    os.environ["PRAXIS_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{args.port}"
    state = WebAppState(args.data_root)
    store = state.session_store
    session, head = seed(store, 8)
    sid = session.session_id
    store.create_sidebar_thread(session_id=sid, thread_id=sid, title="Thread head verification")
    child = _agent_create(sid, head, name="history-worker")
    child.turn.provider_name = "local"
    store.create_agent_thread(sid, child)
    child.turn.status = "success"
    store.finalize_node(child.turn)

    def advance(thread_id: str) -> dict[str, str]:
        parent = store.get_node(sid, store.get_runtime_thread(sid, thread_id).current_turn_id)
        turn = RuntimeState.create(
            session_id=sid,
            thread_id=thread_id,
            parent=parent,
            user_content=[{"type": "text", "text": f"Question after {parent.id}"}],
            provider_name="local",
        )
        store.create_node(turn)
        turn.data[0][-1]["content"] = [{"type": "text", "text": f"New head {turn.id}", "status": "success"}]
        turn.status = "success"
        store.finalize_node(turn)
        publish_frame(state, NodeFrame.snapshot(turn), turn)
        publish_terminal(state, session_id=sid, thread_id=thread_id, turn_id=turn.id, terminal_type="success")
        return {"current_turn_id": turn.id}

    for _ in range(6):
        advance(child.node.thread_id)
    manifest = {"session": sid, "thread": sid, "agent": child.node.thread_id}
    (args.data_root / "thread-head-fixture.json").write_text(json.dumps(manifest), encoding="utf-8")
    app = create_app(state)
    app.post("/fixture/advance/{thread_id}")(advance)
    app.router.routes.insert(0, app.router.routes.pop())
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
