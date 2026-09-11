"""Isolated real UI/settings server with a seeded conversation and no model calls.

Run with the worktree environment:
python -m tests.support.appearance_preview_server --data-root .test-tmp/appearance-data --port 18143
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.domain.runtime_state import RuntimeState
from backend.storage.message_queue import MemoryMessageQueue
from backend.storage.sqlite import SQLiteSessionStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18143)
    args = parser.parse_args()
    root = args.data_root.resolve()
    if root == (Path.home() / ".praxis").resolve():
        parser.error("Use an isolated test data directory, not the live application data.")
    os.environ["PRAXIS_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{args.port},http://localhost:{args.port}"
    queue = MemoryMessageQueue()
    state = WebAppState(root, message_queue=queue)
    store = SQLiteSessionStore(state.paths, state.agent_thread_index)
    if not store.list_sessions():
        session = store.create_session("Appearance test")
        workspace = state.paths.session_workspace(session.session_id)
        (workspace / "notes.py").write_text('print("appearance preview")\n', encoding="utf-8")
        parent = store.ensure_root_node(session.session_id)
        node = RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            id="appearance_preview_turn",
            parent=parent,
            user_content="Check the experiment notes and summarize the next steps.",
            provider_name="appearance-preview",
            cwd=str(workspace),
            data=[
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Check the experiment notes and summarize the next steps.",
                                "status": "success",
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "reasoning",
                                "text": "Compare settings before interpreting the results.",
                                "status": "success",
                            },
                            {
                                "type": "tool_call",
                                "call_id": "appearance_read",
                                "name": "read_file",
                                "arguments": {"path": "notes.py"},
                                "status": "success",
                            },
                            {
                                "type": "tool_result",
                                "call_id": "appearance_read",
                                "tool": "read_file",
                                "content": "Notes loaded successfully.",
                                "status": "success",
                            },
                            {
                                "type": "text",
                                "text": '## Experiment notes\n\nThe notes are ready. Check the **data split**, input length and repeat count before comparing results.\n\n```python\nprint("appearance preview")\n```\n\n| Check | Status |\n| --- | --- |\n| Notes | Complete |\n| Repeat runs | Pending |\n\nNext: verify the configuration, repeat the experiment, and update the summary.',
                                "status": "success",
                            },
                        ],
                    },
                ]
            ],
        )
        payload = node.to_dict()
        payload["status"] = "success"
        store.create_finalized_nodes([RuntimeState.from_dict(payload)])
    for session in store.list_sessions():
        if store.get_sidebar_thread(session.session_id) is None:
            store.create_sidebar_thread(
                session_id=session.session_id, thread_id=session.session_id, title=session.title
            )
    seeded = store.find_node("appearance_preview_turn")
    if isinstance(seeded, RuntimeState) and not seeded.provider_name:
        seeded.provider_name = "appearance-preview"
        store.update_node(seeded)
    app = create_app(state)

    @app.middleware("http")
    async def disable_execution_and_system_maintenance(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.url.path.startswith(("/api/sandbox/", "/api/turns", "/benchmark/"))
        ):
            return JSONResponse(
                {"detail": "Appearance preview: execution and sandbox maintenance are disabled."}, status_code=403
            )
        return await call_next(request)

    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
