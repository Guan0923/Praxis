"""Apply ordered, append-only Turn operations without cloning the transcript."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contract import ITEM_STATUSES, RuntimeStateValidationError, _clone, normalize_content


def apply_turn_delta(payload: dict[str, Any], frame: Mapping[str, Any]) -> None:
    if frame.get("session_id") != payload.get("session_id") or frame.get("turn_id") != payload.get("id"):
        raise RuntimeStateValidationError("Turn delta identity mismatch.")
    patch = frame.get("patch", {})
    forbidden = {"data", "id", "session_id", "thread_id", "parent_id", "parent_session_id", "parent_thread_id"}
    if not isinstance(patch, Mapping) or any(key in forbidden or key not in payload for key in patch):
        raise RuntimeStateValidationError("Invalid Turn delta patch.")
    for operation in frame.get("operations", []):
        try:
            data_idx = operation["data_idx"]
            message_idx = operation["message_idx"]
            if type(data_idx) is not int or type(message_idx) is not int or min(data_idx, message_idx) < 0:
                raise ValueError("Invalid index")
            messages = payload["data"][data_idx]
            kind = operation["op"]
            if kind == "append_message":
                message = operation["message"]
                if message_idx != len(messages) or message.get("role") not in {"user", "assistant"}:
                    raise ValueError("Invalid Message append")
                messages.append(_clone(message))
                continue
            message = messages[message_idx]
            if message.get("role") != "assistant":
                raise ValueError("Items must belong to an assistant")
            items = message["content"]
            item_idx = operation["item_idx"]
            if type(item_idx) is not int or item_idx < 0:
                raise ValueError("Invalid Item index")
            if kind == "append_item":
                if item_idx != len(items):
                    raise ValueError("Invalid Item append")
                items.append(normalize_content([operation["item"]])[0])
            elif kind == "append_text":
                item = items[item_idx]
                delta = operation["delta"]
                if item.get("type") not in {"text", "reasoning"} or not isinstance(delta, str):
                    raise ValueError("Invalid text append")
                item["text"] += delta
            elif kind == "set_item_status" and operation["status"] in ITEM_STATUSES:
                items[item_idx]["status"] = operation["status"]
            else:
                raise ValueError("Unknown Turn operation")
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeStateValidationError("Invalid persisted Turn operation.") from exc
    payload.update(_clone(dict(patch)))
