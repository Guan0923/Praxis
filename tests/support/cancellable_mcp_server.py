"""A local MCP fixture that records actual cancellation in the test directory."""

import asyncio
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="wait",
                description="wait for cancellation",
                input_schema={
                    "type": "object",
                    "properties": {"directory": {"type": "string"}},
                    "required": ["directory"],
                },
            ),
            types.Tool(name="echo", description="check connection", input_schema={"type": "object"}),
        ]
    )


async def call_tool(ctx, params):
    if params.name == "wait":
        directory = Path(params.arguments["directory"])
        (directory / "started").touch()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            (directory / "cancelled").touch()
            raise
    return types.CallToolResult(content=[types.TextContent(type="text", text="alive")])


async def main():
    server = Server("cancellable-test", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
