"""Consume typed input deliveries at explicit runtime safe points."""

from __future__ import annotations

from dataclasses import dataclass

from backend.domain import UserMessage
from backend.domain.input_message import InputMessage
from backend.domain.message_queue import InputDelivery

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent


@dataclass(frozen=True)
class SteeringUpdate:
    delivery: InputDelivery
    message_count: int = 1

    @property
    def content(self) -> str:
        return self.delivery.message.text

    @property
    def delivery_id(self) -> str:
        return self.delivery.delivery_id

    @property
    def source_thread_id(self) -> str:
        return self.delivery.source_thread_id

    @property
    def need_reply(self) -> bool:
        return self.delivery.need_reply


def collect_steering(runtime: AgentRuntime) -> SteeringUpdate | None:
    handler = runtime.services.steering
    if handler is None:
        return None
    deliveries = handler()
    if not deliveries:
        return None
    if len(deliveries) == 1:
        return SteeringUpdate(deliveries[0])
    # Queue-backed mailboxes claim one delivery; embedding callers may batch untracked input.
    if any(item.delivery_id or item.confirm for item in deliveries):
        raise ValueError("Tracked steering must be consumed one delivery at a time.")
    references = tuple(dict.fromkeys(ref for item in deliveries for ref in item.message.references))
    message = InputMessage("\n\n".join(item.message.text for item in deliveries), references)
    return SteeringUpdate(InputDelivery(message), len(deliveries))


def apply_steering(runtime: AgentRuntime, update: SteeringUpdate, *, phase: str) -> None:
    """Append one merged user message and persist it before execution continues."""

    if runtime.stop_requested():
        return
    runtime.exchange.continuation_pending = False
    publish = runtime.services.publish or (lambda _event: None)
    data = {
        "message_count": update.message_count,
        "phase": phase,
        "delivery_id": update.delivery_id,
        "references": update.delivery.message.reference_dicts(),
    }
    publish(RuntimeEvent("steering_received", "In-run user input received", data))

    store = runtime.services.runtime_store
    has_turn_delivery = getattr(store, "has_turn_delivery", None)
    already_applied = (
        bool(update.delivery_id)
        and callable(has_turn_delivery)
        and has_turn_delivery(runtime.state.session_id, update.delivery_id)
    )
    if not already_applied:
        runtime.state.messages.append(UserMessage(content=update.delivery.message.model_text()))
        runtime.run.history = runtime.state.messages
    if store is not None:
        store.append_turn_input(
            runtime.state.session_id,
            runtime.run.run_id,
            update.content,
            delivery_id=update.delivery_id or None,
        )
    # The content travels with the event so the message-tree bridge can
    # persist the steering input as a first-class user node; without it the
    # canonical node projection would silently drop the message.
    publish(RuntimeEvent("steering_applied", "In-run user input applied", {**data, "content": update.content}))
    if update.need_reply:
        register_report = getattr(store, "register_turn_report", None)
        if not callable(register_report) or not update.source_thread_id:
            raise RuntimeError("A reply-requesting Agent message has no canonical report registration path.")
        register_report(runtime.run.turn_id, runtime.run.thread_id, update.source_thread_id)
    runtime.save()
    update.delivery.acknowledge()


def consume_steering(runtime: AgentRuntime, *, phase: str) -> SteeringUpdate | None:
    update = collect_steering(runtime)
    if update is not None:
        apply_steering(runtime, update, phase=phase)
    return update
