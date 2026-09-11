"""Process-local queued messages and mailbox delivery contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .execution_config import TurnExecutionConfig
from .input_message import InputMessage

QueueMessageState = Literal["pending", "dispatched"]
SenderKind = Literal["user", "agent", "system"]
TargetKind = Literal["turn", "turn_start", "thread", "report"]


def queue_utc_now() -> str:
    return datetime.now(UTC).isoformat()


class MessageQueueError(RuntimeError):
    """Base class for stable queue failures."""


class MessageQueueUnavailable(MessageQueueError):
    """The process-owned queue has stopped accepting work."""


class QueueItemNotFound(MessageQueueError):
    """A queued message does not exist in the requested Thread."""


class QueueItemConflict(MessageQueueError):
    """A queued message ID was reused with different immutable input."""


class QueueItemStateConflict(MessageQueueError):
    """The requested mutation is invalid for the queued message state."""


class DeliveryConflict(MessageQueueError):
    """A delivery ID was reused for a different dispatch."""


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    id: str
    thread_id: str
    message: InputMessage
    state: QueueMessageState = "pending"
    created_at: str = field(default_factory=queue_utc_now)
    updated_at: str = field(default_factory=queue_utc_now)

    def __post_init__(self) -> None:
        if not self.id or not self.thread_id:
            raise ValueError("QueuedMessage id and thread_id are required.")
        if self.state not in {"pending", "dispatched"}:
            raise ValueError("QueuedMessage state is invalid.")
        if any(item.source is None for item in self.message.references):
            raise ValueError("Queued user messages require normalized references.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "content": self.message.text,
            "references": self.message.reference_dicts(),
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class TurnStart:
    operation: Literal["create", "rewind"]
    config: TurnExecutionConfig
    parent_id: str | None = None
    queued: bool = False

    def __post_init__(self) -> None:
        if self.operation not in {"create", "rewind"}:
            raise ValueError("Invalid Turn start operation.")


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    delivery_id: str
    sender_kind: SenderKind
    source_thread_id: str
    target_kind: TargetKind
    target_id: str
    session_id: str
    thread_id: str
    message: InputMessage
    source_message_ids: tuple[str, ...]
    created_at: str = field(default_factory=queue_utc_now)
    correlation_id: str | None = None
    attempts: int = 0
    start: TurnStart | None = None
    runtime_config: TurnExecutionConfig | None = None
    need_reply: bool = False
    report_status: Literal["success", "failed"] | None = None

    def __post_init__(self) -> None:
        if not all((self.delivery_id, self.source_thread_id, self.target_id, self.session_id, self.thread_id)):
            raise ValueError("MessageEnvelope identifiers are required.")
        if self.sender_kind not in {"user", "agent", "system"} or self.target_kind not in {
            "turn",
            "turn_start",
            "thread",
            "report",
        }:
            raise ValueError("MessageEnvelope sender or target kind is invalid.")
        if (
            not isinstance(self.source_message_ids, tuple)
            or not self.source_message_ids
            or len(set(self.source_message_ids)) != len(self.source_message_ids)
        ):
            raise ValueError("MessageEnvelope source_message_ids must be non-empty and unique.")
        if isinstance(self.attempts, bool) or self.attempts < 0:
            raise ValueError("MessageEnvelope attempts must be non-negative.")
        if self.sender_kind != "agent" and any(item.source is None for item in self.message.references):
            raise ValueError("User messages require normalized references.")
        if (self.target_kind == "turn_start") != (self.start is not None):
            raise ValueError("Turn-start deliveries require a start command.")


@dataclass(frozen=True, slots=True)
class InputDelivery:
    message: InputMessage
    delivery_id: str = ""
    source_thread_id: str = ""
    need_reply: bool = False
    confirm: Callable[[], None] | None = field(default=None, compare=False, repr=False)

    def acknowledge(self) -> None:
        if self.confirm is not None:
            self.confirm()


@dataclass(frozen=True, slots=True)
class ClaimedEnvelope:
    stream_id: str
    envelope: MessageEnvelope


__all__ = [
    "ClaimedEnvelope",
    "DeliveryConflict",
    "InputDelivery",
    "MessageEnvelope",
    "MessageQueueError",
    "MessageQueueUnavailable",
    "QueueItemConflict",
    "QueueItemNotFound",
    "QueueItemStateConflict",
    "QueuedMessage",
    "TurnStart",
    "queue_utc_now",
]
