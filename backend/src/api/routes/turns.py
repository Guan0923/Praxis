"""Turn-centered tree operations and execution SSE endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from backend.api.error_handlers import error_response
from backend.domain import MessageEnvelope, MessageQueueError, PlanningError
from backend.domain.execution_config import RuntimeConfigUpdate
from backend.domain.input_message import InputMessage
from backend.domain.message_queue import TurnStart
from backend.domain.runtime_state import (
    RuntimeState,
    RuntimeStateValidationError,
    new_node_id,
    new_thread_id,
)
from backend.runtime.conversation.trace import conversation_trace_records
from backend.sandbox import SandboxInitializationError

from ..agent_report_projection import project_turn
from ..chat.routes import (
    RuntimeModelRequest,
    _model_config_snapshot,
    _runtime_stream_lock_registry,
    _startup_failure_message,
)
from ..jsonl import jsonl_download
from ..runtime_event_transport import turn_sse
from ..session_files.routes import _store_for as session_file_store
from ..session_store import require_active_session, session_store
from ..shared.runtime import build_local_application
from ..state import WebAppState
from .turn_models import (
    CreateTurnRequest,
    CurrentDataRequest,
    ForkTurnRequest,
    RewindTurnRequest,
    SteerTurnRequest,
    TurnConfigPatch,
    TurnExecutionConfig,
)
from .turn_support import (
    _queue_http_error,
    _references,
    _stream_turn,
    _turn,
    _user_item,
    create_initial_turn,
    fail_initial_turn,
)

router = APIRouter(prefix="/api/turns", tags=["turns"])


@router.get("")
def list_turns(session_id: str, request: Request) -> list[dict[str, object]]:
    store = session_store(request.app.state.web)
    require_active_session(store, session_id)
    return [
        project_turn(store, item) if isinstance(item, RuntimeState) else item.to_dict()
        for item in store.load_nodes(session_id)
        if item.session_id == session_id
    ]


@router.get("/history")
def turn_history(
    request: Request,
    session_id: str,
    thread_id: str,
    before: str | None = None,
    limit: int = Query(default=5, ge=1, le=100),
) -> dict[str, object]:
    state = request.app.state.web
    store = session_store(state)
    require_active_session(store, session_id)
    try:
        page = store.load_turn_page(session_id, thread_id, before=before, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    state.conversation_cache.touch(session_id, thread_id, [node.id for node in page.turns])
    return {
        "current_turn_id": page.current_turn_id,
        "turns": [project_turn(store, node) for node in page.turns],
        "next_cursor": page.next_cursor,
        "has_more": page.next_cursor is not None,
    }


@router.get("/trace/export")
def export_thread_trace(
    request: Request,
    session_id: str = Query(min_length=1, max_length=200),
    thread_id: str = Query(min_length=1, max_length=200),
) -> StreamingResponse:
    store = session_store(request.app.state.web)
    require_active_session(store, session_id)
    thread = store.get_runtime_thread(session_id, thread_id)
    if thread is None or thread.session_id != session_id:
        raise HTTPException(status_code=404, detail="对话不存在。")
    sidebar = store.get_sidebar_thread(thread_id)
    if sidebar is not None and sidebar.state != "active":
        raise HTTPException(status_code=409, detail="对话已归档或删除，请先恢复。")
    return jsonl_download(conversation_trace_records(store, session_id, thread_id), f"thread-{thread_id}-trace.jsonl")


@router.get("/{turn_id}/trace")
def get_turn_trace(
    turn_id: str,
    session_id: str,
    thread_id: str,
    data_idx: int,
    request: Request,
    after_sequence: int | None = Query(default=None, ge=0),
) -> dict[str, object]:
    trace = request.app.state.web.session_store.load_thread_trace(
        session_id,
        thread_id,
        turn_id,
        data_idx,
        after_sequence=after_sequence,
    )
    return {
        "context": trace.context.to_dict() if trace is not None and after_sequence is None else None,
        "items": [item.to_dict() for item in trace.items] if trace is not None else [],
        "last_sequence": trace.last_sequence if trace is not None else 0,
    }


@router.get("/{turn_id}/stream")
def stream_running_turn(
    turn_id: str,
    request: Request,
    session_id: str,
    thread_id: str | None = None,
    delivery_id: str | None = None,
) -> StreamingResponse:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    require_active_session(store, session_id)
    found = store.get_node(session_id, turn_id)
    if found is None:
        raise HTTPException(status_code=404, detail="未知 Turn。")
    if not isinstance(found, RuntimeState):
        raise HTTPException(status_code=409, detail="根 Turn 仅作为消息树锚点，不能执行 Turn 操作。")
    return StreamingResponse(
        turn_sse(
            state,
            session_id,
            found.thread_id,
            turn_id,
            request.headers.get("last-event-id"),
            delivery_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("", status_code=202)
def create_turn(body: CreateTurnRequest, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    require_active_session(store, body.session_id)
    files = session_file_store(state, body.session_id)
    sidebar = store.get_sidebar_thread(body.thread_id, session_id=body.session_id)
    panel_window = store.active_right_panel_window_for_thread(body.session_id, body.thread_id)
    if (sidebar is None or sidebar.session_id != body.session_id or sidebar.state != "active") and panel_window is None:
        raise HTTPException(status_code=409, detail="Thread 不可用。")
    direct_message = None
    if body.message is not None:
        item = _user_item(body.message)
        direct_message = InputMessage.from_input(str(item["text"]).strip(), _references(item, files))
    config = body.execution_config()
    turn_id = new_node_id()
    delivery_id = f"turn-start:{turn_id}"
    turn = None
    with state.message_queue.admission_lock:
        try:
            state.message_queue.ping()
            message = (
                state.message_queue.pending_message(body.thread_id, body.queued_delivery.message_ids)
                if body.queued_delivery is not None
                else direct_message
            )
            if message is None:
                raise ValueError("Turn message is missing.")
            turn, config = create_initial_turn(
                state,
                store,
                session_id=body.session_id,
                thread_id=body.thread_id,
                parent_id=body.parent_id,
                message=message,
                config=config,
                turn_id=turn_id,
                delivery_id=delivery_id,
            )
            if sidebar is not None:
                store.touch_sidebar_thread_activity(body.thread_id, session_id=body.session_id)
            start = TurnStart("create", config, body.parent_id or None)
            if body.queued_delivery is not None:
                state.message_queue.dispatch(
                    delivery_id=delivery_id,
                    message_ids=body.queued_delivery.message_ids,
                    session_id=body.session_id,
                    thread_id=body.thread_id,
                    turn_id=turn_id,
                    start=start,
                )
            else:
                state.message_queue.dispatch_turn_start(
                    MessageEnvelope(
                        delivery_id=delivery_id,
                        sender_kind="user",
                        source_thread_id=body.thread_id,
                        target_kind="turn_start",
                        target_id=turn_id,
                        session_id=body.session_id,
                        thread_id=body.thread_id,
                        message=message,
                        start=start,
                        source_message_ids=(delivery_id,),
                    )
                )
        except MessageQueueError as exc:
            if turn is not None:
                fail_initial_turn(store, turn, exc)
            return error_response(exc, status_code=_queue_http_error(exc).status_code)
        except Exception as exc:
            if turn is not None:
                fail_initial_turn(store, turn, exc)
            raise
    return project_turn(store, turn)


@router.post("/{turn_id}/rewind", status_code=202)
def rewind_turn(turn_id: str, body: RewindTurnRequest, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id, session_id=getattr(request.state, "operation_session_id", None))
    files = session_file_store(state, source.session_id)
    item = _user_item(body.message)
    try:
        state.message_queue.ping()
    except Exception as exc:
        return error_response(exc, status_code=_queue_http_error(exc).status_code)
    delivery_id = body.delivery_id or f"turn-rewind:{turn_id}:{len(source.data)}"
    envelope = MessageEnvelope(
        delivery_id=delivery_id,
        sender_kind="user",
        source_thread_id=source.thread_id,
        target_kind="turn_start",
        target_id=source.id,
        session_id=source.session_id,
        thread_id=source.thread_id,
        message=InputMessage.from_input(str(item["text"]).strip(), _references(item, files)),
        start=TurnStart("rewind", body.execution_config()),
        source_message_ids=(delivery_id,),
    )
    try:
        state.message_queue.dispatch_turn_start(envelope)
    except Exception as exc:
        return error_response(exc, status_code=_queue_http_error(exc).status_code)
    return {"turn_id": source.id, "delivery_id": delivery_id, "status": "accepted"}


@router.post("/{turn_id}/resume", status_code=202)
def resume_turn(turn_id: str, body: TurnExecutionConfig, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id, session_id=getattr(request.state, "operation_session_id", None))
    if source.status != "paused":
        raise HTTPException(status_code=409, detail="只有 paused Turn 可以恢复。")
    runtime = store.load_runtime(source.session_id, thread_id=source.thread_id)
    run = runtime.current_run if runtime is not None else None
    if run is None or run.turn_id != source.id or run.thread_id != source.thread_id:
        raise HTTPException(status_code=409, detail="当前 Turn 没有对应的可恢复运行状态。")
    try:
        state.message_queue.ping()
    except Exception as exc:
        return error_response(exc, status_code=_queue_http_error(exc).status_code)

    def operation(
        conversation,
        interrupt,
        sink,
        cancel_requested,
        suspend_requested,
        request_parameters,
        steering,
    ):
        return conversation.resume_session(
            source.session_id,
            on_event=sink,
            interrupt=interrupt,
            cancel_requested=cancel_requested,
            suspend_requested=suspend_requested,
            request_parameters=request_parameters,
            steering=steering,
            resume_confirmed=True,
            turn_id=source.id,
        )

    _stream_turn(
        state,
        session_id=source.session_id,
        thread_id=source.thread_id,
        turn_id=source.id,
        message=None,
        source_id=source.id,
        config=body,
        adopt_existing=True,
        operation=operation,
        stream_response=False,
    )
    return {"turn_id": source.id, "status": "accepted"}


@router.post("/{turn_id}/pause")
def pause_turn(turn_id: str, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id, state=state, session_id=getattr(request.state, "operation_session_id", None))
    controller = getattr(state, "active_turn_cancellations", {}).get(turn_id)
    request_pause = getattr(controller, "request_pause", None)
    if callable(request_pause):
        request_pause()
        return {"turn_id": source.id, "status": "accepted"}
    try:
        paused = store.pause_turn(turn_id)
        return {"turn_id": paused.id, "status": paused.status}
    except ValueError as exc:
        return error_response(exc, status_code=409, detail=str(exc))


@router.post("/{turn_id}/steer", status_code=202)
def steer_turn(
    turn_id: str,
    body: SteerTurnRequest,
    request: Request,
) -> dict[str, str]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id, state=state, session_id=getattr(request.state, "operation_session_id", None))
    if source.status != "running":
        raise HTTPException(status_code=409, detail="只有 running Turn 可以接收新输入。")
    active_stream = getattr(state, "active_turn_streams", {}).get(turn_id)
    if active_stream is None:
        raise HTTPException(status_code=409, detail="Turn 执行流已经封闭。")
    controller = getattr(state, "active_turn_cancellations", {}).get(turn_id)

    def dispatch():
        return state.message_queue.dispatch(
            delivery_id=body.delivery_id,
            message_ids=body.message_ids,
            session_id=source.session_id,
            thread_id=source.thread_id,
            turn_id=active_stream.turn_id,
        )

    try:
        if controller is None:
            dispatch()
        else:
            controller.dispatch_steering(dispatch)
    except ValueError as exc:
        return error_response(exc, status_code=409, detail="Turn 正在暂停，消息保留在待发队列。")
    except Exception as exc:
        return error_response(exc, status_code=_queue_http_error(exc).status_code)
    return {"delivery_id": body.delivery_id, "status": "accepted"}


@router.post("/{turn_id}/fork", status_code=201)
def fork_turn(turn_id: str, body: ForkTurnRequest, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    source = _turn(store, turn_id, session_id=getattr(request.state, "operation_session_id", None))
    source_sidebar = store.get_sidebar_thread(source.thread_id, session_id=source.session_id)
    if source_sidebar is None or source_sidebar.session_id != source.session_id:
        raise HTTPException(status_code=409, detail="源 SidebarThread 不可用。")
    thread_id = body.thread_id or new_thread_id()
    try:
        forked = store.fork_turn_node(
            turn_id, new_turn_id=new_node_id(), thread_id=thread_id, session_id=source.session_id
        )
        sidebar = store.create_sidebar_thread(
            session_id=source.session_id,
            thread_id=thread_id,
            title=f"{source_sidebar.title}（分支）",
            title_is_custom=False,
        )
    except (ValueError, RuntimeStateValidationError) as exc:
        return error_response(exc, status_code=409, detail=str(exc))
    page = store.load_turn_page(source.session_id, thread_id)
    return {
        "turn": forked.to_dict(),
        "sidebar_thread": store.sidebar_thread_summary(sidebar).to_dict(),
        "history": {
            "turns": [project_turn(store, node) for node in page.turns],
            "current_turn_id": page.current_turn_id,
            "next_cursor": page.next_cursor,
            "has_more": page.next_cursor is not None,
        },
    }


@router.post("/{turn_id}/compact", status_code=201)
def compact_turn(
    turn_id: str,
    request: Request,
) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id, session_id=getattr(request.state, "operation_session_id", None))
    if source.status != "success":
        raise HTTPException(status_code=409, detail="只有 success Turn 可以压缩。")

    stream_locks = _runtime_stream_lock_registry(state)
    stream_lock = stream_locks["__lock__"]
    stream_keys = stream_locks["keys"]
    stream_key = source.thread_id
    with stream_lock:
        if stream_key in stream_keys:
            raise HTTPException(status_code=409, detail="当前 Thread 已有 running Turn。")
        stream_keys.add(stream_key)

    app = None
    try:
        state.paths.ensure_session(source.session_id)
        workspace = state.paths.session_workspace(source.session_id)
        bound_project = state.projects.session_project(source.session_id, include_removed=False)
        if bound_project is not None and not bound_project.available:
            raise HTTPException(status_code=409, detail="项目 cwd 不可访问，请恢复文件夹后重试。")
        project_cwd = Path(bound_project.cwd).resolve() if bound_project is not None else None
        model_config = _model_config_snapshot(state, provider_name=source.provider_name)
        app = build_local_application(
            state,
            session_id=source.session_id,
            user_preferences=state.agent_preferences(),
            model_config=model_config,
            load_model_config=False,
            workspace=workspace,
            project_id=bound_project.project_id if bound_project is not None else None,
            project_cwd=project_cwd,
            job_registry=state.job_registry,
        )
        conversation = app.open_conversation(source.session_id, thread_id=source.thread_id)
        compacted = conversation.compact_turn(source.id, new_node_id())
        return compacted.to_dict()
    except HTTPException:
        raise
    except SandboxInitializationError as exc:
        return error_response(exc, status_code=503, detail=_startup_failure_message(exc))
    except PlanningError as exc:
        return error_response(exc, status_code=502, detail="上下文压缩失败，请稍后重试。")
    except (KeyError, ValueError, RuntimeStateValidationError) as exc:
        return error_response(exc, status_code=409, detail=str(exc))
    except RuntimeError as exc:
        return error_response(exc, status_code=502, detail="上下文压缩失败，请稍后重试。")
    finally:
        if app is not None:
            app.close()
        with stream_lock:
            stream_keys.discard(stream_key)


@router.patch("/{turn_id}/current-data")
def patch_current_data(turn_id: str, body: CurrentDataRequest, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    try:
        result = store.select_turn_version(body.session_id, turn_id, body.current_data_idx)
        request.app.state.web.application_sync.publish("version.changed", selection=result)
        return result
    except KeyError as exc:
        return error_response(exc, status_code=404, detail="未知 Turn。")
    except RuntimeStateValidationError as exc:
        return error_response(exc, status_code=422, detail=str(exc))
    except ValueError as exc:
        return error_response(exc, status_code=409, detail=str(exc))


@router.patch("/{turn_id}/config")
def patch_turn_config(turn_id: str, body: TurnConfigPatch, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    node = _turn(store, turn_id, session_id=getattr(request.state, "operation_session_id", None))
    if node.status != "running":
        raise HTTPException(status_code=409, detail="只有 running Turn 可以修改运行配置。")
    model = None
    if body.model is not None:
        merged_model = {**node.model, **body.model.model_dump(exclude_none=True)}
        try:
            model = RuntimeModelRequest.model_validate(merged_model)
        except ValueError as exc:
            return error_response(exc, status_code=422, detail=str(exc))
    changes = RuntimeConfigUpdate.model_construct(
        provider_name=body.provider_name,
        model=model,
        permission_mode=body.permission_mode,
        running_mode=body.running_mode,
    )
    bridge = getattr(state, "active_runtime_bridges", {}).get(node.thread_id)
    try:
        updated = bridge.apply_runtime_config(changes) if bridge is not None else None
        if updated is None:
            updated = state.subagent_coordinator.apply_runtime_config(node.session_id, node.thread_id, changes)
        if updated is None:
            writer_node = node.clone()
            for key, value in changes.stored_changes().items():
                setattr(writer_node, key, value)
            writer_node = RuntimeState.from_dict(writer_node.to_dict())
            store.update_node(writer_node)
            updated = writer_node
    except (ValueError, RuntimeStateValidationError) as exc:
        return error_response(exc, status_code=422, detail=str(exc))
    return updated.to_dict()


__all__ = ["TurnExecutionConfig", "router"]
