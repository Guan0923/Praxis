"""Immutable validated execution settings shared by API, queues and runtime."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]
PermissionMode = Literal["read_only", "workspace_write", "full_access"]
RunningMode = Literal["agent", "plan"]


class RuntimeModelRequest(BaseModel):
    """Complete provider-neutral model settings captured at a request boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reasoning_effort: ReasoningEffort
    current_model: str = Field(min_length=1, max_length=500)
    context_length: int = Field(gt=1)
    output_length: int = Field(ge=1)
    thinking: Literal["enable", "disable"]
    temperature: float = Field(ge=0, le=2)

    @model_validator(mode="after")
    def validate_limits(self) -> RuntimeModelRequest:
        if self.context_length <= self.output_length:
            raise ValueError("model.context_length must be greater than model.output_length")
        return self


class TurnExecutionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: RuntimeModelRequest | None = None
    permission_mode: PermissionMode = "read_only"
    running_mode: RunningMode = "agent"
    full_access_acknowledged: StrictBool = False

    def execution_config(self) -> TurnExecutionConfig:
        return TurnExecutionConfig.model_construct(
            provider_name=self.provider_name,
            model=self.model,
            permission_mode=self.permission_mode,
            running_mode=self.running_mode,
            full_access_acknowledged=self.full_access_acknowledged,
        )

    @model_validator(mode="after")
    def validate_full_access(self):
        if self.permission_mode == "full_access" and not self.full_access_acknowledged:
            raise ValueError("full_access requires explicit joint file and network confirmation")
        return self


class RuntimeModelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    current_model: str | None = Field(default=None, min_length=1, max_length=500)
    context_length: int | None = Field(default=None, gt=1)
    output_length: int | None = Field(default=None, ge=1)
    thinking: Literal["enable", "disable"] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)


class TurnConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: RuntimeModelPatch | None = None
    permission_mode: PermissionMode | None = None
    running_mode: RunningMode | None = None
    full_access_acknowledged: StrictBool | None = None

    @model_validator(mode="after")
    def validate_full_access(self):
        if self.permission_mode == "full_access" and self.full_access_acknowledged is not True:
            raise ValueError("full_access requires explicit joint file and network confirmation")
        return self


class RuntimeConfigUpdate(BaseModel):
    """Validated changes queued until the next model/tool boundary."""

    model_config = ConfigDict(frozen=True)
    provider_name: str | None = None
    model: RuntimeModelRequest | RuntimeModelPatch | None = None
    permission_mode: PermissionMode | None = None
    running_mode: RunningMode | None = None

    def merged(self, newer: RuntimeConfigUpdate) -> RuntimeConfigUpdate:
        return RuntimeConfigUpdate.model_construct(
            **{
                key: getattr(newer, key) if getattr(newer, key) is not None else getattr(self, key)
                for key in type(self).model_fields
            }
        )

    def stored_changes(self) -> dict[str, object]:
        return self.model_dump(exclude_none=True)
