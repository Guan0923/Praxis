"""Execution audit records independent of the mutable model transcript."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.domain import TracePersistenceError
from backend.runtime.persistence.recording import turn_trace_audit_value

_CATEGORIES = {
    "handoff_created": "mode_switch",
    "model_request": "model_request",
    "model_response": "model_request",
    "model_error": "model_request",
    "tool_call": "tool_execution",
    "tool_result": "tool_execution",
    "tool_failed": "tool_execution",
    "tool_queued": "tool_execution",
    "tool_indeterminate": "tool_execution",
    "model_retry": "retry",
    "retry": "retry",
    "tool_recovery": "retry",
    "model_repair": "retry",
    "context_compaction_started": "context_compaction",
    "context_compaction_completed": "context_compaction",
    "context_compaction_failed": "context_compaction",
    **{
        kind: "run_control"
        for kind in (
            "run_started",
            "run_resumed",
            "run_suspended",
            "run_interrupted",
            "run_terminated",
            "cancelled",
            "run_finished",
            "error",
        )
    },
}


class _TraceAuditMixin:
    def _close_trace_request(self, reason: str, timestamp: str = "", *, status: str = "failed") -> None:
        if not self._trace_request_open:
            return
        timestamp = timestamp or self.runtime.services.clock()
        self._record_trace_event(
            "model_attempt_ended",
            "model_request",
            {
                **self._trace_request,
                "ended_at": timestamp,
                "status": status,
                "reason": reason,
                "usage": None,
            },
            timestamp=timestamp,
        )
        self._trace_request_open = False

    def _record_trace_event(self, kind: str, category: str, data: Mapping[str, Any], *, timestamp: str = "") -> None:
        turn = self.assistant or self.last_node
        if self.runtime is None or turn is None or self.trace_persistence_failed:
            return
        append = getattr(self.store, "append_turn_trace_item", None)
        if not callable(append):
            return
        timestamp = timestamp or self.runtime.services.clock()
        record = turn_trace_audit_value(
            {
                "type": "runtime_event",
                "event": kind,
                "category": category,
                "timestamp": timestamp,
                "data": dict(data),
                "status": data.get("status") if data.get("status") in {"running", "success", "failed"} else "success",
            }
        )
        try:
            self.writer.flush()
            stored = append(
                turn.session_id,
                turn.id,
                turn.current_data_idx,
                message_idx=None,
                item_idx=None,
                role="runtime",
                item=record,
                completed_at=timestamp,
            )
            if stored is None:
                raise RuntimeError("Runtime event was not persisted.")
        except Exception as exc:
            self.trace_persistence_failed = True
            raise TracePersistenceError("Local trace event persistence failed; the Turn was stopped.") from exc

    def _audit_runtime_event(self, kind: str, message: str, data: Mapping[str, Any], timestamp: str) -> None:
        category = _CATEGORIES.get(kind)
        if category is None:
            return
        timestamp = timestamp or self.runtime.services.clock()
        payload = dict(data)
        payload["reason"] = data.get("reason") or data.get("stop_reason") or message
        if category == "model_request":
            # The context and completed Items already contain the transcript.
            for key in ("messages", "tools", "message", "wire_request", "wire_response"):
                payload.pop(key, None)
            if kind == "model_request":
                self._trace_request_open = True
                transport = data.get("transport") or {}
                parameters = data.get("request_parameters") or {}
                attempt = transport.get("attempt", 1)
                exchange_id = data.get("exchange_id")
                reasoning = parameters.get("reasoning")
                effort = parameters.get("reasoning_effort")
                if effort is None and isinstance(reasoning, Mapping):
                    effort = reasoning.get("effort")
                self._trace_request = {
                    "request_id": f"{data.get('run_id', self.run_id)}:{exchange_id}:{attempt}",
                    "exchange_id": exchange_id,
                    "attempt": attempt,
                    "model": data.get("model"),
                    "reasoning_effort": effort,
                    "started_at": timestamp,
                }
            payload = {**payload, **self._trace_request}
            payload["status"] = {"model_request": "running", "model_response": "success", "model_error": "failed"}[kind]
            if kind != "model_request":
                self._trace_request_open = False
                payload["ended_at"] = timestamp
                payload.setdefault("usage", None)
        elif category == "mode_switch":
            handoff = self.runtime.run.handoff
            payload.update(
                old_mode=self._trace_effective_mode or self.running_mode,
                new_mode=handoff.mode if handoff is not None else data.get("mode"),
                source="plan_review",
                requested_at=timestamp,
                effective_at=None,
            )
        elif category == "retry":
            payload.update(
                failed_request_id=self._trace_request.get("request_id"),
                exchange_id=data.get("exchange_id") or self._trace_request.get("exchange_id"),
                retry_number=data.get("attempt"),
            )
            if kind == "model_retry":
                self._close_trace_request(str(payload["reason"]), timestamp)
                payload["failed_request"] = {**self._trace_request, "ended_at": timestamp, "status": "failed"}
        elif category == "tool_execution":
            call_id = str(data.get("call_id") or "")
            if kind == "tool_call":
                self._trace_tools.setdefault(
                    call_id,
                    {
                        "tool": data.get("tool") or data.get("name") or message,
                        "arguments": data.get("arguments"),
                        "started_at": data.get("started_at") or timestamp,
                    },
                )
            payload = {**self._trace_tools.get(call_id, {}), **payload}
            payload["status"] = {"tool_call": "running", "tool_queued": "queued", "tool_result": "success"}.get(
                kind, "failed"
            )
            if kind in {"tool_result", "tool_failed", "tool_indeterminate"}:
                payload.update(ended_at=timestamp, result=data.get("result", data.get("content", message)))
                self._trace_tools.pop(call_id, None)
        elif category == "context_compaction":
            if kind == "context_compaction_started":
                self._trace_compaction = {**data, "started_at": timestamp}
            payload = {**self._trace_compaction, **payload}
            payload.setdefault("estimated_tokens_before", None)
            payload.setdefault("estimated_tokens_after", None)
            payload["status"] = (
                "running" if kind.endswith("started") else "failed" if kind.endswith("failed") else "success"
            )
            if not kind.endswith("started"):
                payload["ended_at"] = timestamp
                self._trace_compaction = {}
        elif kind in {"cancelled", "run_suspended", "run_interrupted", "run_terminated", "run_finished", "error"}:
            self._close_trace_request(
                str(payload["reason"]), timestamp, status="failed" if kind == "error" else "interrupted"
            )
        self._record_trace_event(kind, category, payload, timestamp=timestamp)
