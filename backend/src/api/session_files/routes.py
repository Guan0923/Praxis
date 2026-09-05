"""Session file upload, search, content preview, and deletion endpoints.

All routes are local and session-scoped.  Project roots come from the
session's effective cwd (``WebAppState.session_workspace``), so an external
project folder is searchable while uploads always live inside the session's
own workspace.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..session_store import require_active_session, session_store
from ..state import WebAppState
from .store import SessionFileConflict, SessionFileError, SessionFileStore

router = APIRouter(prefix="/api")

_CONTENT_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"})


class SaveEditorFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["workspace", "project"]
    path: str = Field(min_length=1, max_length=4000)
    content: str
    encoding: str = Field(min_length=1, max_length=32)
    bom: bool = False
    newline: Literal["\n", "\r\n", "\r"] = "\n"
    version: str = Field(min_length=1, max_length=200)
    force: bool = False


class CreateEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["workspace", "project"]
    parent_path: str = Field(min_length=1, max_length=4000)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["file", "directory"]


class RenameEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["workspace", "project"]
    path: str = Field(min_length=1, max_length=4000)
    name: str = Field(min_length=1, max_length=200)


class MoveEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["workspace", "project"]
    path: str = Field(min_length=1, max_length=4000)
    target_source: Literal["workspace", "project"]
    target_parent_path: str = Field(min_length=1, max_length=4000)


def _store_for(state: WebAppState, session_id: str) -> SessionFileStore:
    """Build the session file store with the validated project root."""

    store = session_store(state)
    require_active_session(store, session_id)
    paths = state.paths
    paths.ensure_session(session_id)
    project_root = None
    try:
        if state.projects.session_project(session_id) is not None:
            project_root = state.session_workspace(session_id)
    except RuntimeError:
        # A removed/unavailable project cwd keeps uploads usable while
        # project-root search and content resolve to nothing.
        project_root = None
    return SessionFileStore(paths, session_id, project_root=project_root)


def _file_error(exc: SessionFileError) -> HTTPException:
    if isinstance(exc, SessionFileConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if "不存在" in str(exc) or "无效" in str(exc):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/sessions/{session_id}/files/roots")
def file_roots(session_id: str, request: Request) -> list[dict[str, object]]:
    try:
        return _store_for(request.app.state.web, session_id).roots()
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.get("/sessions/{session_id}/files/tree")
def list_file_directory(session_id: str, request: Request, source: str, path: str) -> list[dict[str, object]]:
    try:
        return _store_for(request.app.state.web, session_id).list_directory(source, path)
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.get("/sessions/{session_id}/files/editor")
def read_editor_file(
    session_id: str,
    request: Request,
    source: str,
    path: str,
    encoding: str | None = None,
) -> dict[str, object]:
    try:
        return _store_for(request.app.state.web, session_id).read_editor_file(source, path, encoding)
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.put("/sessions/{session_id}/files/editor")
def save_editor_file(session_id: str, body: SaveEditorFileRequest, request: Request) -> dict[str, object]:
    try:
        return _store_for(request.app.state.web, session_id).write_editor_file(
            body.source,
            body.path,
            content=body.content,
            encoding=body.encoding,
            bom=body.bom,
            newline=body.newline,
            expected_version=body.version,
            force=body.force,
        )
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.post("/sessions/{session_id}/files/entries", status_code=201)
def create_file_entry(session_id: str, body: CreateEntryRequest, request: Request) -> dict[str, object]:
    try:
        return _store_for(request.app.state.web, session_id).create_entry(
            body.source, body.parent_path, body.name, body.kind
        )
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.patch("/sessions/{session_id}/files/entries/rename")
def rename_file_entry(session_id: str, body: RenameEntryRequest, request: Request) -> dict[str, object]:
    try:
        return _store_for(request.app.state.web, session_id).rename_entry(body.source, body.path, body.name)
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.patch("/sessions/{session_id}/files/entries/move")
def move_file_entry(session_id: str, body: MoveEntryRequest, request: Request) -> dict[str, object]:
    try:
        return _store_for(request.app.state.web, session_id).move_entry(
            body.source, body.path, body.target_source, body.target_parent_path
        )
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.delete("/sessions/{session_id}/files/entries")
def recycle_file_entry(session_id: str, request: Request, source: str, path: str) -> dict[str, str]:
    try:
        _store_for(request.app.state.web, session_id).recycle_entry(source, path)
    except SessionFileError as exc:
        raise _file_error(exc) from exc
    return {"deleted": path}


@router.post("/sessions/{session_id}/files")
def upload_session_files(
    session_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
) -> list[dict[str, object]]:
    """Upload a bounded multipart batch; every file lands immediately."""

    state: WebAppState = request.app.state.web
    try:
        store = _store_for(state, session_id)
        items = [(upload.filename, upload.file) for upload in files]
        return store.store_batch(items)
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.get("/sessions/{session_id}/files")
def search_session_files(
    session_id: str,
    request: Request,
    q: str = "",
    limit: int = 20,
) -> list[dict[str, object]]:
    state: WebAppState = request.app.state.web
    try:
        store = _store_for(state, session_id)
        return store.search(q, limit)
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.get("/sessions/{session_id}/files/content")
def session_file_content(
    session_id: str,
    request: Request,
    source: str,
    path: str,
    download: bool = False,
) -> Response:
    """Return a local preview (inline) or download (attachment)."""

    state: WebAppState = request.app.state.web
    try:
        store = _store_for(state, session_id)
        resolved = store.resolve(source, path)
        mime = _mime_type(resolved.name)
        is_image = _is_image_file(resolved.name, mime)
        disposition = "attachment" if download or not is_image else "inline"
        response = FileResponse(
            resolved,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'{disposition}; filename="{_safe_filename(resolved.name)}"',
                "Cache-Control": "private, no-store",
            },
        )
        return response
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.head("/sessions/{session_id}/files/content")
def session_file_content_head(
    session_id: str,
    request: Request,
    source: str,
    path: str,
    download: bool = False,
) -> Response:
    """Validate a local file reference without returning its body."""

    state: WebAppState = request.app.state.web
    try:
        store = _store_for(state, session_id)
        resolved = store.resolve(source, path)
        mime = _mime_type(resolved.name)
        is_image = _is_image_file(resolved.name, mime)
        disposition = "attachment" if download or not is_image else "inline"
        return Response(
            status_code=200,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'{disposition}; filename="{_safe_filename(resolved.name)}"',
                "Cache-Control": "private, no-store",
            },
        )
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.delete("/sessions/{session_id}/files")
def delete_session_file(
    session_id: str,
    request: Request,
    source: str,
    path: str,
) -> dict[str, str]:
    """Delete one session-uploaded file; project files are never deletable."""

    state: WebAppState = request.app.state.web
    if source != "upload":
        raise HTTPException(status_code=403, detail="只能删除会话上传的文件。")
    try:
        store = _store_for(state, session_id)
        store.delete_upload(path)
    except SessionFileError as exc:
        raise _file_error(exc) from exc
    return {"deleted": path}


def _mime_type(name: str) -> str:
    import mimetypes

    guessed, _encoding = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _is_image_file(name: str, mime: str) -> bool:
    return name.rsplit(".", 1)[-1].casefold() in _CONTENT_IMAGE_EXTENSIONS or mime.startswith("image/")


def _safe_filename(name: str) -> str:
    return name.replace('"', "_").replace("\\", "_").replace("/", "_")


__all__ = ["router"]
