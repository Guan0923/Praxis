"""Validated request models shared by Turn and Agent Thread routes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.domain.execution_config import RuntimeModelPatch, TurnConfigPatch, TurnExecutionConfig


class QueuedDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str = Field(min_length=1, max_length=200)
    message_ids: list[str] = Field(min_length=1, max_length=100)


class SteerTurnRequest(QueuedDeliveryRequest):
    pass


class CreateTurnRequest(TurnExecutionConfig):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    parent_id: str = Field(default="", max_length=200)
    delivery_id: str | None = Field(default=None, min_length=1, max_length=200)
    message: dict[str, object] | None = None
    queued_delivery: QueuedDeliveryRequest | None = None

    @model_validator(mode="after")
    def validate_message_source(self):
        if (self.message is None) == (self.queued_delivery is None):
            raise ValueError("message and queued_delivery are mutually exclusive")
        return self


class RewindTurnRequest(TurnExecutionConfig):
    message: dict[str, object]
    delivery_id: str | None = Field(default=None, min_length=1, max_length=200)


class CurrentDataRequest(BaseModel):
    current_data_idx: int = Field(ge=0)


class ForkTurnRequest(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=200)
    thread_id: str | None = Field(default=None, min_length=1, max_length=200)


__all__ = [
    "CreateTurnRequest",
    "CurrentDataRequest",
    "ForkTurnRequest",
    "QueuedDeliveryRequest",
    "RewindTurnRequest",
    "RuntimeModelPatch",
    "SteerTurnRequest",
    "TurnConfigPatch",
    "TurnExecutionConfig",
]
