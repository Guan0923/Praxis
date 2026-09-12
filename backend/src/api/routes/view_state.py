"""Durable UI state and application-wide change subscription."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..session_store import require_active_session, session_store

router = APIRouter(prefix="/api", tags=["view-state"])


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["project", "upload", "workspace"]
    path: str = Field(max_length=4096)
    display_path: str = Field(max_length=4096)


class UploadedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uid: str = Field(max_length=128)
    name: str = Field(max_length=4096)
    isImage: bool
    status: Literal["done"]
    percent: Literal[100]
    path: str = Field(max_length=4096)
    displayPath: str | None = Field(default=None, max_length=4096)


class ReadingPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    top: float = Field(ge=0, allow_inf_nan=False)
    messageId: str | None = Field(default=None, max_length=256)
    offset: float = Field(allow_inf_nan=False)
    atBottom: bool = False


class ViewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: str = Field(default="", max_length=1_000_000)
    references: list[Reference] = Field(default_factory=list, max_length=200)
    uploads: list[UploadedFile] = Field(default_factory=list, max_length=200)
    reading: ReadingPosition | None = None
    expanded: dict[str, bool] = Field(default_factory=dict, max_length=2000)


def require_thread(request: Request, session_id: str, thread_id: str):
    store = session_store(request.app.state.web)
    require_active_session(store, session_id)
    sidebar = store.get_sidebar_thread(thread_id, session_id=session_id)
    if sidebar is None:
        node = store.get_thread_node(session_id, thread_id)
        if node is not None:
            sidebar = store.get_sidebar_thread(node.root_thread_id, session_id=session_id)
    if sidebar is not None and sidebar.state != "active":
        raise HTTPException(409, "Conversation is archived or deleted.")
    if sidebar is None and store.get_runtime_thread(session_id, thread_id) is None:
        raise HTTPException(404, "Unknown conversation.")
    return store


@router.get("/view-state/{session_id}/{thread_id}")
def read_view(session_id: str, thread_id: str, request: Request):
    return require_thread(request, session_id, thread_id).get_view_state(session_id, thread_id)


@router.patch("/view-state/{session_id}/{thread_id}")
def patch_view(session_id: str, thread_id: str, body: ViewPatch, request: Request):
    result = require_thread(request, session_id, thread_id).patch_view_state(
        session_id, thread_id, body.model_dump(exclude_unset=True)
    )
    request.app.state.web.application_sync.publish("view.changed", view=result)
    return result


@router.get("/application-events")
async def application_events(request: Request):
    sync = request.app.state.web.application_sync
    return StreamingResponse(
        sync.events(request.headers.get("last-event-id") or request.query_params.get("cursor"), request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/conversation-target/{session_id}/{root_thread_id}/{thread_id}")
def conversation_target(session_id: str, root_thread_id: str, thread_id: str, request: Request):
    store = require_thread(request, session_id, thread_id)
    node = store.get_thread_node(session_id, thread_id)
    if node is None or node.root_thread_id != root_thread_id:
        raise HTTPException(404, "Conversation does not belong to this root.")
    return {"session_id": session_id, "thread_id": thread_id, "root_thread_id": root_thread_id}
