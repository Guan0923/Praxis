"""User-level MCP JSON configuration and saved-connection tests."""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, StrictBool

from backend.api.error_handlers import error_response
from backend.mcp.client import start_external_tools
from backend.mcp.config import McpSettings
from backend.mcp.json_config import McpJsonDocument
from backend.mcp.settings import McpServerNotFound, McpSettingsStore


class SecretSafeMcpRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": [
                            {"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()
                        ]
                    },
                )

        return route


router = APIRouter(prefix="/api/settings/mcp", tags=["settings"], route_class=SecretSafeMcpRoute)


class EnabledPayload(BaseModel):
    enabled: StrictBool


def _store(request: Request) -> McpSettingsStore:
    return McpSettingsStore(request.app.state.web.paths)


def _payload(request: Request) -> dict[str, object]:
    return {
        "enabled": bool(request.app.state.web.settings.capability_config()["mcp"]),
        **_store(request).document(),
    }


@router.get("")
def get_mcp_settings(request: Request):
    return _payload(request)


@router.put("")
def save_mcp_settings(body: McpJsonDocument, request: Request):
    try:
        document = _store(request).replace_document(body)
    except ValueError as exc:
        return error_response(exc, status_code=422)
    return {"enabled": bool(request.app.state.web.settings.capability_config()["mcp"]), **document}


@router.put("/enabled")
def update_mcp_enabled(body: EnabledPayload, request: Request):
    request.app.state.web.settings.update_capability_config({"mcp": body.enabled})
    return _payload(request)


@router.post("/servers/{name}/test")
def test_mcp_server(name: str, request: Request):
    state = request.app.state.web
    try:
        server = replace(_store(request).server(name), enabled=True)
    except McpServerNotFound as exc:
        return error_response(exc, status_code=404)
    resources = start_external_tools((server,), McpSettings.from_config(state.settings.config_store.read()))
    try:
        tools = sorted(f"mcp_{name}_{item.name}" for item in resources.manager.definitions[name])
        details = resources.manager.describe(name)
        return {"tools": tools, "count": details["counts"]["tools"], **details}
    finally:
        resources.close()
