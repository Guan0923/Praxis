"""Value comparisons and ordered merging for the process-owned queue."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from hashlib import sha256

from backend.domain.input_message import FileReference, InputMessage
from backend.domain.message_queue import MessageEnvelope, QueuedMessage


def _dispatch_identity(thread_id: str, turn_id: str, message_ids: Sequence[str]) -> tuple[str, str, tuple[str, ...]]:
    return thread_id, turn_id, tuple(sorted(message_ids))


def _envelope_identity(envelope: MessageEnvelope) -> MessageEnvelope:
    # Retain content comparison without keeping the body alive after eviction.
    message = replace(envelope.message, text=sha256(envelope.message.text.encode("utf-8")).hexdigest())
    return replace(envelope, message=message, attempts=0)


def _merge(messages: Sequence[QueuedMessage]) -> InputMessage:
    references: list[FileReference] = []
    seen: set[tuple[str | None, str]] = set()
    for queued in messages:
        for reference in queued.message.references:
            key = (reference.source, reference.path)
            if key not in seen:
                seen.add(key)
                references.append(reference)
    return InputMessage("\n\n".join(item.message.text for item in messages), tuple(references))


def _same_create(left: QueuedMessage, right: QueuedMessage) -> bool:
    return left.id == right.id and left.thread_id == right.thread_id and left.message == right.message
