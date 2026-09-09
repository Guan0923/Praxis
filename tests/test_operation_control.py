from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.chat.interrupts import registry
from backend.api.operation_control import OperationControl
from backend.api.state import WebAppState


class _Socket:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.messages.append(payload)


def test_browser_write_requires_operation_protocol_but_local_cli_does_not(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".praxis")
    with TestClient(create_app(state)) as client:
        rejected = client.post(
            "/api/sidebar-threads",
            json={},
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == "浏览器写请求缺少操作协议头。"

        accepted = client.post("/api/sidebar-threads", json={})
        assert accepted.status_code == 201


def test_window_owner_reconnect_generation_and_waiter_promotion() -> None:
    async def scenario() -> None:
        control = OperationControl(reconnect_grace_seconds=0.01)
        first_socket = _Socket()
        second_socket = _Socket()
        token, first_generation = await control.connect("first", None, first_socket)
        _, second_generation = await control.connect("second", None, second_socket)
        assert await control.claim_session("first", first_generation, "session-one") is True
        assert await control.claim_session("second", second_generation, "session-one") is False

        replacement_socket = _Socket()
        _, replacement_generation = await control.connect("first", token, replacement_socket)
        await control.disconnect("first", first_generation)
        await asyncio.sleep(0.02)
        assert await control.is_session_owner("first", "session-one") is True

        await control.open_group("first", replacement_generation, "session:session-one")
        await control.operation(
            window_id="first",
            generation=replacement_generation,
            group="session:session-one",
            session_id="session-one",
        )
        await control.disconnect("first", replacement_generation)
        await asyncio.sleep(0.02)
        assert await control.is_session_owner("first", "session-one") is False
        assert await control.is_session_owner("second", "session-one") is False
        await control.finish_operation("first", "session-one")
        await asyncio.sleep(0.02)
        assert await control.is_session_owner("second", "session-one") is True
        assert second_socket.messages[-1] == {
            "type": "session.ownership",
            "session_id": "session-one",
            "writable": True,
        }

    asyncio.run(scenario())


def test_operation_seq_ack_rejects_duplicate_without_repeating_write(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".praxis")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        session_id = sidebar["session_id"]
        thread_id = sidebar["thread_id"]
        with client.websocket_connect(
            "/api/window-control/ws?window_id=window-one",
            headers={"Origin": "http://127.0.0.1:5173"},
        ) as websocket:
            ready = websocket.receive_json()
            websocket.send_json({"type": "session.claim", "session_id": session_id, "request_id": "claim"})
            assert websocket.receive_json()["writable"] is True
            group = f"session:{session_id}"
            websocket.send_json({"type": "group.open", "group": group, "request_id": "group"})
            opened = websocket.receive_json()
            headers = {
                "X-Praxis-Window": "window-one",
                "X-Praxis-Window-Generation": str(ready["generation"]),
                "X-Praxis-Operation-Group": group,
                "X-Praxis-Session": session_id,
                "X-Praxis-Seq": str(opened["seq"]),
                "X-Praxis-Ack": str(opened["ack"]),
            }
            body = {"id": "5c1a7d20-2ff3-45f6-bf59-5dbb13dbf58f", "content": "only once", "references": []}
            first = client.post(f"/api/sidebar-threads/{thread_id}/queued-messages", json=body, headers=headers)
            duplicate = client.post(f"/api/sidebar-threads/{thread_id}/queued-messages", json=body, headers=headers)

            assert first.status_code == 201
            assert duplicate.status_code == 409
            assert duplicate.json()["code"] == "operation_sequence_mismatch"
            assert len(client.get(f"/api/sidebar-threads/{thread_id}/queued-messages").json()) == 1


def test_operation_body_reaches_endpoint_and_old_generation_is_rejected(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".praxis")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        session_id = sidebar["session_id"]
        with client.websocket_connect(
            "/api/window-control/ws?window_id=window-one",
            headers={"Origin": "http://127.0.0.1:5173"},
        ) as websocket:
            ready = websocket.receive_json()
            websocket.send_json({"type": "session.claim", "session_id": session_id, "request_id": "claim"})
            assert websocket.receive_json()["writable"] is True
            group = f"session:{session_id}"
            websocket.send_json({"type": "group.open", "group": group, "request_id": "group"})
            opened = websocket.receive_json()
            headers = {
                "X-Praxis-Window": "window-one",
                "X-Praxis-Window-Generation": str(ready["generation"]),
                "X-Praxis-Operation-Group": group,
                "X-Praxis-Session": session_id,
                "X-Praxis-Seq": str(opened["seq"]),
                "X-Praxis-Ack": str(opened["ack"]),
            }
            decision_id = "decision-body-replay"
            pending = registry.register(decision_id)
            try:
                response = client.post(
                    "/api/decisions",
                    json={"decision_id": decision_id, "choice": "continue", "session_id": session_id},
                    headers=headers,
                )
            finally:
                registry.discard(decision_id)
            assert response.status_code == 200
            assert pending.result["choice"] == "continue"

            stale_headers = {
                **headers,
                "X-Praxis-Seq": response.headers["X-Praxis-Ack"],
                "X-Praxis-Ack": str(int(response.headers["X-Praxis-Seq"]) + 1),
            }
            token = ready["token"]
            with client.websocket_connect(
                f"/api/window-control/ws?window_id=window-one&token={token}",
                headers={"Origin": "http://127.0.0.1:5173"},
            ) as replacement:
                replacement_ready = replacement.receive_json()
                assert replacement_ready["generation"] > ready["generation"]
                stale = client.post(
                    "/api/decisions",
                    json={"decision_id": "late", "choice": "continue", "session_id": session_id},
                    headers=stale_headers,
                )
                assert stale.status_code == 409
                assert "窗口连接已失效" in stale.json()["detail"]
