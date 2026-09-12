"""MCP transports with application-owned processes and HTTP credentials."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx2
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from backend.domain import error_report, redact_sensitive_text
from backend.tools import ToolError

from ..config import McpServerConfig, McpSettings
from ..controlled_stdio import controlled_stdio_client
from .adapters import _parameters, _resolve_environment_reference


class _SecretSafeLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.getMessage())
        record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            record.msg += "\n" + error_report(record.exc_info[1])["traceback"]
            record.exc_info = None
            record.exc_text = None
        return True


for _logger_name in ("httpx2", "mcp.client.sse", "mcp.client.streamable_http"):
    logging.getLogger(_logger_name).addFilter(_SecretSafeLogs())


def _http_client(**kwargs):
    return httpx2.AsyncClient(follow_redirects=False, verify=True, **kwargs)


@asynccontextmanager
async def connection_transport(server: McpServerConfig, settings: McpSettings):
    if server.transport == "stdio":
        async with controlled_stdio_client(_parameters(server)) as streams:
            yield streams
        return
    headers = dict(server.headers or {})
    for name, reference in (server.header_refs or {}).items():
        value = _resolve_environment_reference(reference, name)
        if "\r" in value or "\n" in value or len(value) > 4096:
            raise ToolError("Invalid MCP credential header value.")
        headers[name] = value
    url = _resolve_environment_reference(server.url_ref, "url") if server.url_ref else server.url
    failures: list[httpx2.HTTPStatusError] = []

    async def inspect_response(response: httpx2.Response) -> None:
        failures.clear()
        if response.status_code >= 300 and (
            response.status_code in {401, 403}
            or not response.headers.get("content-type", "").startswith("application/json")
        ):
            try:
                response.raise_for_status()
            except httpx2.HTTPStatusError as exc:
                failures.append(exc)

    def create_http(**kwargs):
        return _http_client(event_hooks={"response": [inspect_response]}, **kwargs)

    try:
        if server.transport == "sse":
            async with sse_client(
                url,
                headers=headers,
                timeout=server.timeout,
                sse_read_timeout=server.timeout,
                httpx_client_factory=create_http,
            ) as streams:
                yield streams
            return
        async with create_http(headers=headers, timeout=httpx2.Timeout(server.timeout)) as http:
            async with streamable_http_client(url, http_client=http) as streams:
                yield streams
    except Exception:
        # The SDK converts non-JSON HTTP failures into a generic JSON-RPC error.
        if failures:
            raise failures[-1] from None
        raise
