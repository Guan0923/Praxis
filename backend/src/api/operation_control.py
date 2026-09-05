"""In-memory browser window ownership and operation sequencing."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException


def _sequence() -> int:
    return secrets.randbelow(2**52 - 1) + 1


@dataclass
class OperationSequence:
    client_seq: int = field(default_factory=_sequence)
    server_seq: int = field(default_factory=_sequence)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class WindowConnection:
    token: str
    generation: int = 0
    connected: bool = False
    socket: Any | None = None
    release_task: asyncio.Task[None] | None = None
    sessions: set[str] = field(default_factory=set)
    groups: dict[str, OperationSequence] = field(default_factory=dict)
    active_session_operations: int = 0


class OperationControl:
    """Own live browser identities; nothing here survives a backend restart."""

    def __init__(self, *, reconnect_grace_seconds: float = 3.0) -> None:
        self.reconnect_grace_seconds = reconnect_grace_seconds
        self._lock = asyncio.Lock()
        self._windows: dict[str, WindowConnection] = {}
        self._session_owners: dict[str, str] = {}
        self._session_waiters: dict[str, list[str]] = {}

    async def connect(self, window_id: str, token: str | None, socket: Any) -> tuple[str, int]:
        async with self._lock:
            current = self._windows.get(window_id)
            if current is None:
                current = WindowConnection(token=secrets.token_urlsafe(24))
                self._windows[window_id] = current
            elif not token or not secrets.compare_digest(token, current.token):
                raise HTTPException(status_code=409, detail="窗口身份已失效，请刷新页面。")
            if current.release_task is not None:
                current.release_task.cancel()
                current.release_task = None
            current.generation += 1
            current.connected = True
            current.socket = socket
            return current.token, current.generation

    async def disconnect(self, window_id: str, generation: int) -> None:
        async with self._lock:
            current = self._windows.get(window_id)
            if current is None or current.generation != generation:
                return
            current.connected = False
            current.socket = None
            current.release_task = asyncio.create_task(self._release_after_grace(window_id, generation))

    async def _release_after_grace(self, window_id: str, generation: int) -> None:
        try:
            await asyncio.sleep(self.reconnect_grace_seconds)
            while True:
                notifications: list[tuple[Any, dict[str, object]]] = []
                async with self._lock:
                    current = self._windows.get(window_id)
                    if current is None or current.connected or current.generation != generation:
                        return
                    if current.active_session_operations == 0:
                        for session_id in tuple(current.sessions):
                            if self._session_owners.get(session_id) == window_id:
                                self._session_owners.pop(session_id, None)
                                promoted = self._promote_waiter_locked(session_id)
                                if promoted is not None:
                                    notifications.append(promoted)
                        for waiters in self._session_waiters.values():
                            while window_id in waiters:
                                waiters.remove(window_id)
                        self._windows.pop(window_id, None)
                        break
                await asyncio.sleep(0.01)
            for socket, payload in notifications:
                await self._send(socket, payload)
        except asyncio.CancelledError:
            return

    def _promote_waiter_locked(self, session_id: str) -> tuple[Any, dict[str, object]] | None:
        waiters = self._session_waiters.get(session_id, [])
        while waiters:
            window_id = waiters.pop(0)
            candidate = self._windows.get(window_id)
            if candidate is None or not candidate.connected or candidate.socket is None:
                continue
            self._session_owners[session_id] = window_id
            candidate.sessions.add(session_id)
            return candidate.socket, {
                "type": "session.ownership",
                "session_id": session_id,
                "writable": True,
            }
        self._session_waiters.pop(session_id, None)
        return None

    async def claim_session(self, window_id: str, generation: int, session_id: str) -> bool:
        async with self._lock:
            current = self._require_connection_locked(window_id, generation)
            owner = self._session_owners.get(session_id)
            if owner is None:
                self._session_owners[session_id] = window_id
                current.sessions.add(session_id)
                return True
            if owner == window_id:
                current.sessions.add(session_id)
                return True
            waiters = self._session_waiters.setdefault(session_id, [])
            if window_id not in waiters:
                waiters.append(window_id)
            return False

    async def open_group(self, window_id: str, generation: int, group: str) -> OperationSequence:
        async with self._lock:
            current = self._require_connection_locked(window_id, generation)
            return current.groups.setdefault(group, OperationSequence())

    async def operation(
        self,
        *,
        window_id: str,
        generation: int,
        group: str,
        session_id: str | None,
    ) -> tuple[OperationSequence, asyncio.Lock]:
        async with self._lock:
            current = self._windows.get(window_id)
            if current is None or not current.connected or current.generation != generation:
                raise HTTPException(status_code=409, detail="窗口连接已失效，请刷新页面。")
            if session_id is not None and self._session_owners.get(session_id) != window_id:
                raise HTTPException(status_code=423, detail="当前 session 正在另一个窗口对话。")
            state = current.groups.get(group)
            if state is None:
                raise HTTPException(status_code=409, detail="操作序号尚未握手。")
            if session_id is not None:
                current.active_session_operations += 1
            return state, state.lock

    async def finish_operation(self, window_id: str, session_id: str | None) -> None:
        if session_id is None:
            return
        async with self._lock:
            current = self._windows.get(window_id)
            if current is not None and current.active_session_operations > 0:
                current.active_session_operations -= 1

    async def is_session_owner(self, window_id: str, session_id: str, generation: int | None = None) -> bool:
        async with self._lock:
            current = self._windows.get(window_id)
            return bool(
                current
                and current.connected
                and (generation is None or current.generation == generation)
                and self._session_owners.get(session_id) == window_id
            )

    def _require_connection_locked(self, window_id: str, generation: int) -> WindowConnection:
        current = self._windows.get(window_id)
        if current is None or not current.connected or current.generation != generation:
            raise HTTPException(status_code=409, detail="窗口连接已失效，请刷新页面。")
        return current

    @staticmethod
    async def _send(socket: Any, payload: dict[str, object]) -> None:
        try:
            await socket.send_json(payload)
        except Exception:
            return


OP_WINDOW_HEADER = "X-Mini-Agent-Window"
OP_WINDOW_GENERATION_HEADER = "X-Mini-Agent-Window-Generation"
OP_GROUP_HEADER = "X-Mini-Agent-Operation-Group"
OP_SESSION_HEADER = "X-Mini-Agent-Session"
OP_SEQ_HEADER = "X-Mini-Agent-Seq"
OP_ACK_HEADER = "X-Mini-Agent-Ack"


def operation_headers(request) -> tuple[str, int, str, str | None, int, int] | None:
    window_id = request.headers.get(OP_WINDOW_HEADER)
    if not window_id:
        return None
    group = request.headers.get(OP_GROUP_HEADER)
    raw_generation = request.headers.get(OP_WINDOW_GENERATION_HEADER)
    raw_seq = request.headers.get(OP_SEQ_HEADER)
    raw_ack = request.headers.get(OP_ACK_HEADER)
    if not group or raw_generation is None or raw_seq is None or raw_ack is None:
        raise HTTPException(status_code=400, detail="操作协议头不完整。")
    try:
        generation = int(raw_generation)
        seq = int(raw_seq)
        ack = int(raw_ack)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="操作序号无效。") from exc
    return window_id, generation, group, request.headers.get(OP_SESSION_HEADER), seq, ack


async def canonical_operation_session(request, state, claimed_session: str | None) -> str | None:
    """Resolve a session-scoped URL/body and reject a false ownership claim."""

    from .session_store import session_store

    path = request.url.path
    resolved: str | None = None
    direct = re.match(r"^/api/(?:sessions|right-panel)/([^/]+)", path)
    if direct:
        resolved = direct.group(1)
    store = session_store(state)
    thread = re.match(r"^/api/sidebar-threads/([^/]+)", path)
    if thread and thread.group(1) != "order":
        thread_id = thread.group(1)
        item = store.get_sidebar_thread(thread_id)
        if item is not None:
            resolved = item.session_id
        else:
            for summary in store.list_sessions(state="all"):
                panel = store.active_right_panel_window_for_thread(summary.session_id, thread_id)
                if panel is not None:
                    resolved = panel.session_id
                    break
    turn = re.match(r"^/api/turns/([^/]+)", path)
    if turn:
        item = store.find_node(turn.group(1))
        if item is not None:
            resolved = item.session_id
    agent_thread = re.match(r"^/api/agent-threads/([^/]+)", path)
    if agent_thread:
        resolved = state.agent_thread_index.session_for_thread(agent_thread.group(1))
    if path in {"/api/turns", "/api/decisions"} or agent_thread:
        body_bytes = await request.body()

        async def replay_body():
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        request._receive = replay_body
        try:
            body = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        if isinstance(body, dict) and isinstance(body.get("session_id"), str):
            resolved = body["session_id"]
    if resolved is not None and claimed_session != resolved:
        raise HTTPException(status_code=409, detail="操作所属 session 与请求声明不一致。")
    return resolved or claimed_session
