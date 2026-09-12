"""Turn lookup, user-item normalization, queue errors, and SSE startup."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from backend.configuration import ConfigurationError
from backend.domain import (
    DeliveryConflict,
    MessageQueueUnavailable,
    QueueItemConflict,
    QueueItemNotFound,
    QueueItemStateConflict,
    error_report,
    safe_error_message,
)
from backend.domain.execution_config import RuntimeModelRequest, TurnExecutionConfig
from backend.domain.input_message import InputMessage
from backend.domain.runtime_state import (
    NodeWriter,
    RuntimeRootState,
    RuntimeState,
    terminal_error_payload,
)

from ..chat import routes as chat_routes
from ..chat.routes import _model_config_snapshot, _stream
from ..session_files.store import SessionFileError, SessionFileStore
from ..state import WebAppState


def _turn(store, turn_id: str) -> RuntimeState:
    item = store.find_node(turn_id)
    if item is None:
        raise HTTPException(status_code=404, detail="未知 Turn。")
    if isinstance(item, RuntimeRootState):
        raise HTTPException(status_code=409, detail="根 Turn 仅作为消息树锚点，不能执行 Turn 操作。")
    return item


def _user_item(message: Mapping[str, object]) -> dict[str, object]:
    if message.get("role") != "user":
        raise HTTPException(status_code=422, detail="message.role 必须为 user。")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
        raise HTTPException(status_code=422, detail="user Message 必须恰好包含一个 Item。")
    item = dict(content[0])
    if item.get("type") != "text" or not isinstance(item.get("text"), str):
        raise HTTPException(status_code=422, detail="当前交互要求一个 text Item。")
    if not str(item["text"]).strip() and not item.get("references"):
        raise HTTPException(status_code=422, detail="text 或文件引用至少需要一个。")
    item["status"] = "success"
    return item


def _references(item: Mapping[str, object], files: SessionFileStore) -> list[dict[str, str]]:
    raw = item.get("references", [])
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="references 必须为 list。")
    if any(not isinstance(value, Mapping) for value in raw):
        raise HTTPException(status_code=422, detail="无效的文件引用。")
    try:
        return files.normalize_references(raw)
    except (SessionFileError, ConfigurationError) as exc:
        exc.api_status_code = 422
        raise


def _queue_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MessageQueueUnavailable):
        return HTTPException(status_code=503, detail="message_queue_unavailable")
    if isinstance(exc, QueueItemNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (DeliveryConflict, QueueItemConflict, QueueItemStateConflict)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="message_queue_error")


def create_initial_turn(
    state: WebAppState,
    store,
    *,
    session_id: str,
    thread_id: str,
    parent_id: str,
    message: InputMessage,
    config: TurnExecutionConfig,
    turn_id: str,
    delivery_id: str,
) -> tuple[RuntimeState, TurnExecutionConfig]:
    parent = store.get_node(session_id, parent_id) if parent_id else store.ensure_root_node(session_id)
    if parent is None:
        raise ValueError("Unknown parent Turn.")
    state.paths.ensure_session(session_id)
    workspace = state.paths.session_workspace(session_id)
    bound_project = state.projects.session_project(session_id, include_removed=False)
    if bound_project is not None and not bound_project.available:
        raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
    project_cwd = Path(bound_project.cwd).resolve() if bound_project is not None else None
    selected = _model_config_snapshot(state, provider_name=config.provider_name)
    model = config.model or RuntimeModelRequest(
        reasoning_effort="medium",
        current_model=selected.model,
        context_length=selected.context_size,
        output_length=selected.max_tokens,
        thinking="enable",
        temperature=selected.temperature,
    )
    resolved_config = TurnExecutionConfig.model_construct(
        provider_name=config.provider_name or selected.provider_name,
        model=model,
        permission_mode=config.permission_mode,
        running_mode=config.running_mode,
        full_access_acknowledged=config.full_access_acknowledged,
    )
    turn = RuntimeState.create(
        session_id=session_id,
        thread_id=thread_id,
        id=turn_id,
        parent=parent,
        user_content=[message.to_item()],
        provider_name=resolved_config.provider_name or selected.provider_name,
        model=model.model_dump(),
        permission_mode=resolved_config.permission_mode,
        running_mode=resolved_config.running_mode,
        cwd=str(workspace),
        project_cwd=str(project_cwd) if project_cwd is not None else "",
    )
    turn.data[0][0]["delivery_id"] = delivery_id
    turn.__post_init__()
    return NodeWriter(store).create(turn), resolved_config


def fail_initial_turn(store, turn: RuntimeState, exc: Exception) -> RuntimeState:
    writer = NodeWriter(store)
    current = writer.current(turn.session_id, turn.id)
    current = writer.append_item(
        current,
        terminal_error_payload(
            "server",
            safe_error_message(exc),
            retryable=False,
            code=type(exc).__name__,
            error_report=error_report(exc),
        ),
    )
    return writer.finalize(current, "failed")


def _stream_turn(
    state: WebAppState,
    *,
    session_id: str,
    thread_id: str,
    turn_id: str,
    message: InputMessage | None,
    source_id: str | None,
    config: TurnExecutionConfig,
    adopt_existing: bool = False,
    precreated: bool = False,
    operation=None,
    initial_delivery=None,
    stream_response: bool = True,
) -> StreamingResponse | None:
    stream = _stream(
        state,
        message,
        application_builder=chat_routes.build_local_application,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        source_node_id=source_id,
        adopt_existing=adopt_existing,
        precreated=precreated,
        config=config,
        user_preferences=state.agent_preferences(),
        model_config=_model_config_snapshot(state),
        operation=operation,
        initial_delivery=initial_delivery,
        subscribe=stream_response,
    )
    if not stream_response:
        return None
    return StreamingResponse(stream, media_type="text/event-stream")


__all__ = [
    "_queue_http_error",
    "_references",
    "_stream_turn",
    "_turn",
    "_user_item",
    "create_initial_turn",
    "fail_initial_turn",
]
