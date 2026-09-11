"""Typed mailbox deliveries consumed at runtime safe boundaries."""

from __future__ import annotations

from functools import partial

from backend.domain.message_queue import ClaimedEnvelope, InputDelivery

from .memory_message_queue import MemoryMessageQueue


def _delivery(claimed: ClaimedEnvelope, queue: MemoryMessageQueue) -> InputDelivery:
    envelope = claimed.envelope
    return InputDelivery(
        envelope.message,
        envelope.delivery_id,
        envelope.source_thread_id,
        envelope.need_reply,
        partial(queue.ack, claimed),
    )


class TurnMailbox:
    def __init__(self, queue: MemoryMessageQueue, turn_id: str, consumer: str) -> None:
        self.queue = queue
        self.turn_id = turn_id
        self.consumer = consumer
        self.closed = False

    def take(self) -> list[InputDelivery]:
        if self.closed:
            return []
        claimed = self.queue.claim(self.turn_id, self.consumer)
        return [_delivery(claimed, self.queue)] if claimed is not None else []

    def close(self) -> None:
        self.closed = True


class AgentMailbox(TurnMailbox):
    def __init__(self, queue: MemoryMessageQueue, turn_id: str, thread_id: str, consumer: str) -> None:
        super().__init__(queue, turn_id, consumer)
        self.thread_id = thread_id

    def take(self) -> list[InputDelivery]:
        deliveries = super().take()
        if deliveries or self.closed:
            return deliveries
        claimed = self.queue.claim_thread(self.thread_id, self.consumer)
        return [_delivery(claimed, self.queue)] if claimed is not None else []


__all__ = ["AgentMailbox", "TurnMailbox"]
