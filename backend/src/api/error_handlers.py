"""Uniform secret-safe exception projection for HTTP boundaries."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.domain import error_report, redact_sensitive_text, root_error, safe_error_message


def error_response(
    error: BaseException,
    *,
    status_code: int = 500,
    detail: object = None,
    headers: dict[str, str] | None = None,
    code: str | None = None,
) -> JSONResponse:
    status_code = getattr(error, "api_status_code", status_code)
    content = {"detail": safe_error_message(error), "error_report": error_report(error)}
    if code is not None:
        content["code"] = code
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    """Preserve HTTP control metadata while exposing only safe root messages."""

    @app.middleware("http")
    async def report_unhandled(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as error:
            # Consume handled HTTP failures so the server does not log an unsafe traceback.
            report = error_report(error)
            logging.getLogger(__name__).error("HTTP request failed: %s", report)
            return JSONResponse(
                status_code=getattr(error, "api_status_code", 500),
                content={"detail": report["message"], "error_report": report},
            )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        detail: Any = error.detail
        if root_error(error) is not error:
            detail = safe_error_message(error)
        elif isinstance(detail, str):
            detail = redact_sensitive_text(detail)
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": detail, "error_report": error_report(error)},
            headers=error.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_error(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"detail": safe_error_message(error), "error_report": error_report(error)}
        )


__all__ = ["install_error_handlers"]
