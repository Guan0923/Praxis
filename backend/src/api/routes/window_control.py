"""Browser-window ownership control channel."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..security import LocalWebSettings, browser_origin_allowed

router = APIRouter(prefix="/api/window-control", tags=["window-control"])


@router.websocket("/ws")
async def window_control(websocket: WebSocket) -> None:
    settings = LocalWebSettings.from_env()
    if not browser_origin_allowed(websocket.headers.get("origin"), settings):
        await websocket.close(code=1008)
        return
    window_id = websocket.query_params.get("window_id", "").strip()
    token = websocket.query_params.get("token")
    if not window_id:
        await websocket.close(code=1008)
        return
    control = websocket.app.state.web.operation_control
    try:
        token, generation = await control.connect(window_id, token, websocket)
    except Exception:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    await websocket.send_json({"type": "ready", "token": token, "generation": generation})
    try:
        while True:
            payload = await websocket.receive_json()
            request_id = str(payload.get("request_id") or "")
            kind = payload.get("type")
            if kind == "session.claim":
                session_id = str(payload.get("session_id") or "").strip()
                writable = bool(session_id) and await control.claim_session(window_id, generation, session_id)
                await websocket.send_json(
                    {
                        "type": "session.ownership",
                        "request_id": request_id,
                        "session_id": session_id,
                        "writable": writable,
                    }
                )
            elif kind == "group.open":
                group = str(payload.get("group") or "").strip()
                if not group:
                    await websocket.close(code=1003)
                    return
                state = await control.open_group(window_id, generation, group)
                await websocket.send_json(
                    {
                        "type": "group.ready",
                        "request_id": request_id,
                        "group": group,
                        "seq": state.client_seq,
                        "ack": state.server_seq,
                    }
                )
            else:
                await websocket.close(code=1003)
                return
    except WebSocketDisconnect:
        pass
    finally:
        await control.disconnect(window_id, generation)


__all__ = ["router"]
