"""User-facing MCP JSON schema and secret-safe validation."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from backend.tools import ToolError

from .config import valid_environment_name, valid_header_name, valid_server_name, validate_http_url

STORED_SECRET = "<stored-secret>"


class McpJsonServer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["stdio", "sse", "streamableHttp"]
    disabled: StrictBool = False
    timeout: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    command: str | None = Field(default=None, max_length=2000)
    args: list[str] = Field(default_factory=list, max_length=128)
    env: dict[str, str] = Field(default_factory=dict, max_length=128)
    cwd: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=16000)
    headers: dict[str, str] = Field(default_factory=dict, max_length=128)

    @field_validator("args")
    @classmethod
    def validate_args(cls, values: list[str]) -> list[str]:
        if any(len(value) > 2000 for value in values):
            raise ValueError("MCP arguments must not exceed 2000 characters")
        return values

    @field_validator("env")
    @classmethod
    def validate_env(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not valid_environment_name(name) or len(value) > 4096 for name, value in values.items()):
            raise ValueError("Invalid MCP environment name or value length")
        return values

    @field_validator("headers")
    @classmethod
    def validate_header_values(cls, values: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, value in values.items():
            if not valid_header_name(name) or name.lower() in result:
                raise ValueError("Invalid, reserved or duplicate MCP header name")
            if len(value) > 4096 or "\r" in value or "\n" in value:
                raise ValueError("Invalid MCP header value")
            result[name.lower()] = value
        return result

    @model_validator(mode="after")
    def validate_transport(self) -> Self:
        if self.type == "stdio":
            if not self.command or not self.command.strip():
                raise ValueError("stdio requires command")
            if self.url is not None or self.headers:
                raise ValueError("stdio cannot include url or headers")
            if self.cwd is not None and not self.cwd.strip():
                raise ValueError("cwd must be a non-empty directory")
        else:
            if self.command is not None or self.args or self.env or self.cwd is not None:
                raise ValueError("HTTP connections cannot include command, args, env or cwd")
            try:
                validate_http_url(self.url)
            except ToolError as exc:
                raise ValueError(str(exc)) from None
        return self


class McpJsonDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mcpServers: dict[str, McpJsonServer] = Field(max_length=128)

    @field_validator("mcpServers")
    @classmethod
    def validate_names(cls, values: dict[str, McpJsonServer]) -> dict[str, McpJsonServer]:
        if any(not valid_server_name(name) for name in values):
            raise ValueError("MCP server names must use 1-64 letters, digits, '_' or '-'")
        return values
