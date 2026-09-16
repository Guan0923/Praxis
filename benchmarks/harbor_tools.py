"""Synchronous Praxis tools backed by Harbor task environments."""

from __future__ import annotations

import asyncio
import base64
import shlex
from collections.abc import Coroutine
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Event
from typing import Any, TypeVar

from harbor.environments.base import BaseEnvironment

from backend.tools.base import Tool, ToolError, ToolInvocationContext
from backend.tools.registry import ToolRegistry

T = TypeVar("T")


class HarborTools:
    def __init__(self, environment: BaseEnvironment, loop: asyncio.AbstractEventLoop, stopped: Event) -> None:
        self.environment = environment
        self.loop = loop
        self.stopped = stopped

    def _wait(self, coroutine: Coroutine[Any, Any, T], context: ToolInvocationContext) -> T:
        if self.stopped.is_set() or (context.cancel_requested and context.cancel_requested()):
            coroutine.close()
            raise ToolError("Harbor trial stopped.")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        unregister = context.register_abort(future.cancel) if context.register_abort else None
        try:
            while True:
                if self.stopped.is_set() or (context.cancel_requested and context.cancel_requested()):
                    future.cancel()
                    raise ToolError("Harbor trial stopped.")
                try:
                    return future.result(timeout=0.2)
                except FutureTimeout:
                    if future.done():
                        try:
                            return future.result()
                        except TimeoutError:
                            raise ToolError("Harbor tool command timed out.") from None
        finally:
            if unregister is not None:
                unregister()

    def execute(self, context: ToolInvocationContext, command: str, timeout_seconds: int = 60) -> str:
        result = self._wait(self.environment.exec(command=command, timeout_sec=timeout_seconds), context)
        output = (result.stdout or "") + (result.stderr or "")
        return f"exit_code={result.return_code}\n{output[:50000]}"

    def read(self, context: ToolInvocationContext, path: str) -> str:
        return self.execute(context, "head -c 50000 -- " + shlex.quote(path))

    def write(self, context: ToolInvocationContext, path: str, content: str) -> str:
        # Exec respects the task user, unlike docker cp. Bound each Windows argv.
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        for offset in range(0, max(1, len(encoded)), 12000):
            redirect = ">" if offset == 0 else ">>"
            chunk = shlex.quote(encoded[offset : offset + 12000])
            command = f"printf %s {chunk} | base64 -d {redirect} {shlex.quote(path)}"
            result = self._wait(self.environment.exec(command=command, timeout_sec=60), context)
            if result.return_code != 0:
                raise ToolError(f"Container write failed: {(result.stderr or result.stdout or '')[:2000]}")
        return "File written in the task container."

    def registry(self) -> ToolRegistry:
        text = {"type": "string"}

        def tool(name, description, handler, properties, required, *, read_only=False):
            return Tool(
                name,
                description,
                lambda **kwargs: handler(ToolInvocationContext(), **kwargs),
                {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
                requires_confirmation=not read_only,
                read_only=read_only,
                context_handler=handler,
            )

        return ToolRegistry(
            [
                tool(
                    "container_exec",
                    "Run Bash in the Harbor task container, never on the host.",
                    self.execute,
                    {"command": text, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600}},
                    ["command"],
                ),
                tool(
                    "container_read_file",
                    "Read at most 50000 bytes from a file in the task container.",
                    self.read,
                    {"path": text},
                    ["path"],
                    read_only=True,
                ),
                tool(
                    "container_write_file",
                    "Write a UTF-8 file in the task container.",
                    self.write,
                    {"path": text, "content": {"type": "string", "maxLength": 1000000}},
                    ["path", "content"],
                ),
            ]
        )
