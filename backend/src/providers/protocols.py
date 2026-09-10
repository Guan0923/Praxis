"""Provider-neutral adapters for Chat Completions, Responses, and Messages."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from typing import Any

from backend.domain import (
    AssistantMessage,
    ChatMessage,
    SystemMessage,
    ToolMessage,
    ToolSpec,
    UserMessage,
    safe_error_message,
)
from backend.runtime.core.context import AgentRuntime, PreparedResponse

from .chat_completions import ChatCompletions
from .config import ModelConfig
from .errors import ModelResponseError, ProviderOutputError


class ChatCompletionsAdapter(ChatCompletions):
    """OpenAI-compatible Chat Completions adapter."""

    pass


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _estimate(messages: list[ChatMessage], tools: list[ToolSpec], parameters: Mapping[str, Any]) -> int:
    payload = {
        "messages": [getattr(message, "content", "") or "" for message in messages],
        "tools": [{"name": spec.name, "parameters": spec.parameters} for spec in tools],
    }
    input_tokens = max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)
    max_tokens = parameters.get("max_tokens", 0)
    return input_tokens + (int(max_tokens) if isinstance(max_tokens, int) and max_tokens > 0 else 0)


def _parse_tool_call(name: Any, call_id: Any, arguments: Any, *, incomplete: bool) -> ToolMessage | None:
    try:
        if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
            raise ValueError("Tool call name and id must be non-empty strings.")
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(parsed, Mapping):
            raise ValueError("Tool call arguments must be a complete JSON object.")
    except (TypeError, ValueError) as exc:
        if incomplete:
            return None
        raise ProviderOutputError(safe_error_message(exc)) from exc
    return ToolMessage(name=name, call_id=call_id, arguments=dict(parsed), status="pending")


def _message_block_index(event: Mapping[str, Any]) -> int:
    index = event.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ProviderOutputError("Messages content block requires a non-negative integer index.")
    return index


class ResponsesAdapter:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    @property
    def context_size(self) -> int:
        return self.config.context_size

    @property
    def endpoint(self) -> str:
        return self.config.endpoint

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    @property
    def timeout_seconds(self) -> int:
        return self.config.timeout_seconds

    @property
    def operation(self) -> str:
        return "responses"

    def estimate_tokens(self, messages, tools, request_parameters):
        return _estimate(messages, tools, request_parameters)

    estimate_input_tokens = estimate_tokens

    def prepare_request(self, runtime: AgentRuntime) -> dict[str, Any]:
        config = runtime.request_config()
        parameters = dict(config.get("request_parameters") or {})
        overrides = runtime.exchange.context.get("request_parameters")
        if isinstance(overrides, Mapping):
            parameters.update(overrides)
        required_tool_name = parameters.get("required_tool_name")
        items: list[dict[str, Any]] = []
        for message in runtime.exchange.messages or runtime.state.messages:
            if isinstance(message, SystemMessage | UserMessage):
                items.append({"role": message.role, "content": message.content or ""})
            elif isinstance(message, AssistantMessage):
                if message.content:
                    items.append({"role": "assistant", "content": message.content})
                for tool in message.tool_messages:
                    items.append(
                        {
                            "type": "function_call",
                            "id": tool.call_id,
                            "call_id": tool.call_id,
                            "name": tool.name,
                            "arguments": json.dumps(tool.arguments, ensure_ascii=False, separators=(",", ":")),
                        }
                    )
                    if tool.status != "pending" and tool.content is not None:
                        items.append(
                            {
                                "type": "function_call_output",
                                "call_id": tool.call_id,
                                "output": tool.content,
                            }
                        )
        model_snapshot = config.get("model_snapshot") if isinstance(config.get("model_snapshot"), Mapping) else {}
        payload: dict[str, Any] = {
            "model": str(config.get("model") or model_snapshot.get("current_model") or self.config.model),
            "input": items,
            "stream": runtime.exchange.stream,
            "max_output_tokens": int(
                parameters.get("max_tokens", model_snapshot.get("output_length", self.config.max_tokens))
            ),
        }
        temperature = parameters.get("temperature", model_snapshot.get("temperature", self.config.temperature))
        if temperature is not None:
            payload["temperature"] = temperature
        if parameters.get("reasoning_effort") is not None:
            payload["reasoning"] = {"effort": parameters["reasoning_effort"]}
        if parameters.get("thinking") == {"type": "disabled"}:
            payload.pop("reasoning", None)
        tools = runtime.exchange.allowed_tools
        if tools:
            payload["tools"] = [
                {"type": "function", "name": spec.name, "description": spec.description, "parameters": spec.parameters}
                for spec in tools
            ]
        if isinstance(required_tool_name, str) and required_tool_name:
            payload["tool_choice"] = {"type": "function", "name": required_tool_name}
        if runtime.exchange.output_mode == "json":
            payload["text"] = {"format": {"type": "json_object"}}
        runtime.exchange.request = payload
        return payload

    def prepare_response(self, runtime: AgentRuntime) -> PreparedResponse:
        raw = runtime.exchange.raw_response
        parsed = self._parse_stream(runtime, raw) if not isinstance(raw, Mapping) else self._parse_json(raw)
        runtime.exchange.prepared_response = parsed
        runtime.state.turn_usage = parsed.usage
        return parsed

    def _parse_json(self, data: Mapping[str, Any]) -> PreparedResponse:
        status = data.get("status")
        details = data.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, Mapping) else None
        incomplete = status == "incomplete" and reason == "max_output_tokens"
        if (status == "incomplete" and not incomplete) or status in {"failed", "cancelled"} or data.get("error"):
            error = data.get("error")
            detail = error.get("message") if isinstance(error, Mapping) else None
            raise ModelResponseError(
                str(detail or f"Responses generation did not complete: {reason or status or 'error'}."),
                diagnostics={
                    "finish_reason": status,
                    "incomplete_details": details,
                    "error": error,
                    "usage": data.get("usage"),
                },
            )
        output = data.get("output")
        if not isinstance(output, list):
            raise ProviderOutputError("Responses output must be an array.")
        content = ""
        reasoning = ""
        tools: list[ToolMessage] = []
        for item in output:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type == "message":
                content += _text_content(item.get("content"))
            elif item_type in {"reasoning", "summary"}:
                reasoning += _text_content(item.get("summary") or item.get("content"))
            elif item_type == "function_call":
                if incomplete and item.get("status") not in {None, "completed"}:
                    continue
                tool = _parse_tool_call(
                    item.get("name"),
                    item.get("call_id") or item.get("id"),
                    item.get("arguments"),
                    incomplete=incomplete,
                )
                if tool is not None:
                    tools.append(tool)
        usage = data.get("usage")
        usage = dict(usage) if isinstance(usage, Mapping) else None
        return PreparedResponse(
            AssistantMessage(
                content=content or None,
                reasoning=reasoning or None,
                tool_messages=tools,
                provider_options={"responses": {"response": copy.deepcopy(dict(data))}},
            ),
            usage=usage,
            response_id=str(data.get("id")) if data.get("id") else None,
            model=str(data.get("model")) if data.get("model") else None,
            finish_reason=str(data.get("status")) if data.get("status") else None,
            provider_metadata={"type": data.get("object", "response"), "incomplete_details": copy.deepcopy(details)},
            incomplete_reason="output_limit" if incomplete else None,
        )

    def _parse_stream(self, runtime: AgentRuntime, events: Iterable[dict[str, Any]]) -> PreparedResponse:
        text: list[str] = []
        reasoning: list[str] = []
        final: Mapping[str, Any] | None = None
        for event in events:
            kind = str(event.get("__sse_event") or event.get("type") or "")
            if kind == "response.output_text.delta":
                delta = event.get("delta", "")
                if isinstance(delta, str):
                    text.append(delta)
                    if runtime.exchange.on_content:
                        runtime.exchange.on_content(delta)
            elif kind in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
                delta = event.get("delta", "")
                if isinstance(delta, str):
                    reasoning.append(delta)
                    if runtime.exchange.on_reasoning:
                        runtime.exchange.on_reasoning(delta)
            elif kind in {"response.completed", "response.incomplete", "response.failed", "response.cancelled"}:
                candidate = event.get("response")
                if isinstance(candidate, Mapping):
                    final = {**candidate, "status": kind.removeprefix("response.")}
            elif kind == "error":
                raise ModelResponseError(
                    str(event.get("message") or "Responses stream returned an error."),
                    diagnostics={"provider_error": dict(event)},
                )
        if final is not None:
            parsed = self._parse_json(final)
            if text:
                parsed.message.content = "".join(text)
            if reasoning:
                parsed.message.reasoning = "".join(reasoning)
            return parsed
        raise ModelResponseError("Responses stream ended without a terminal response event.")


class MessagesAdapter:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    @property
    def context_size(self) -> int:
        return self.config.context_size

    @property
    def endpoint(self) -> str:
        return self.config.endpoint

    @property
    def headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    @property
    def timeout_seconds(self) -> int:
        return self.config.timeout_seconds

    @property
    def operation(self) -> str:
        return "messages"

    def estimate_tokens(self, messages, tools, request_parameters):
        return _estimate(messages, tools, request_parameters)

    estimate_input_tokens = estimate_tokens

    def prepare_request(self, runtime: AgentRuntime) -> dict[str, Any]:
        config = runtime.request_config()
        parameters = dict(config.get("request_parameters") or {})
        overrides = runtime.exchange.context.get("request_parameters")
        if isinstance(overrides, Mapping):
            parameters.update(overrides)
        required_tool_name = parameters.get("required_tool_name")
        system: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        for message in runtime.exchange.messages or runtime.state.messages:
            if isinstance(message, SystemMessage):
                system.append({"type": "text", "text": message.content or ""})
            elif isinstance(message, UserMessage):
                messages.append({"role": "user", "content": [{"type": "text", "text": message.content or ""}]})
            elif isinstance(message, AssistantMessage):
                blocks: list[dict[str, Any]] = []
                results: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for tool in message.tool_messages:
                    blocks.append({"type": "tool_use", "id": tool.call_id, "name": tool.name, "input": tool.arguments})
                    if tool.status != "pending" and tool.content is not None:
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool.call_id,
                                "content": tool.content,
                                **({"is_error": True} if tool.status == "failed" else {}),
                            }
                        )
                if blocks:
                    messages.append({"role": "assistant", "content": blocks})
                if results:
                    messages.append({"role": "user", "content": results})
        model_snapshot = config.get("model_snapshot") if isinstance(config.get("model_snapshot"), Mapping) else {}
        payload: dict[str, Any] = {
            "model": str(config.get("model") or model_snapshot.get("current_model") or self.config.model),
            "messages": messages,
            "max_tokens": int(
                parameters.get("max_tokens", model_snapshot.get("output_length", self.config.max_tokens))
            ),
            "stream": runtime.exchange.stream,
        }
        temperature = parameters.get("temperature", model_snapshot.get("temperature", self.config.temperature))
        if temperature is not None:
            payload["temperature"] = temperature
        thinking = parameters.get("thinking")
        if isinstance(thinking, Mapping) and thinking.get("type") == "enabled":
            payload["thinking"] = {"type": "enabled"}
        if system:
            payload["system"] = system
        if runtime.exchange.allowed_tools:
            payload["tools"] = [
                {"name": spec.name, "description": spec.description, "input_schema": spec.parameters}
                for spec in runtime.exchange.allowed_tools
            ]
        if isinstance(required_tool_name, str) and required_tool_name:
            payload["tool_choice"] = {"type": "tool", "name": required_tool_name}
        runtime.exchange.request = payload
        return payload

    def prepare_response(self, runtime: AgentRuntime) -> PreparedResponse:
        raw = runtime.exchange.raw_response
        parsed = self._parse_stream(runtime, raw) if not isinstance(raw, Mapping) else self._parse_json(raw)
        runtime.exchange.prepared_response = parsed
        runtime.state.turn_usage = parsed.usage
        return parsed

    def _parse_json(self, data: Mapping[str, Any]) -> PreparedResponse:
        stop_reason = data.get("stop_reason")
        incomplete = stop_reason == "max_tokens"
        if data.get("type") == "error" or stop_reason in {"refusal", "content_filter"}:
            error = data.get("error")
            detail = error.get("message") if isinstance(error, Mapping) else None
            raise ModelResponseError(
                str(detail or f"Messages generation did not complete: {stop_reason or 'error'}."),
                diagnostics={"finish_reason": stop_reason, "error": error, "usage": data.get("usage")},
            )
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderOutputError("Messages response content must be an array.")
        text: list[str] = []
        reasoning: list[str] = []
        tools: list[ToolMessage] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "text":
                text.append(str(block.get("text") or ""))
            elif kind in {"thinking", "redacted_thinking"}:
                reasoning.append(str(block.get("thinking") or block.get("text") or ""))
            elif kind == "tool_use":
                tool = _parse_tool_call(
                    block.get("name"),
                    block.get("id"),
                    block.get("input"),
                    incomplete=incomplete,
                )
                if tool is not None:
                    tools.append(tool)
        usage = data.get("usage")
        usage = dict(usage) if isinstance(usage, Mapping) else None
        return PreparedResponse(
            AssistantMessage(
                content="".join(text) or None,
                reasoning="".join(reasoning) or None,
                tool_messages=tools,
                provider_options={"messages": {"response": copy.deepcopy(dict(data))}},
            ),
            usage=usage,
            response_id=str(data.get("id")) if data.get("id") else None,
            model=str(data.get("model")) if data.get("model") else None,
            finish_reason=str(data.get("stop_reason")) if data.get("stop_reason") else None,
            provider_metadata={"type": "message"},
            incomplete_reason="output_limit" if incomplete else None,
        )

    def _parse_stream(self, runtime: AgentRuntime, events: Iterable[dict[str, Any]]) -> PreparedResponse:
        text: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        stopped_blocks: set[int] = set()
        usage: dict[str, Any] = {}
        header: dict[str, Any] = {}
        stop_reason: str | None = None
        stopped = False
        for event in events:
            kind = str(event.get("__sse_event") or event.get("type") or "")
            if kind == "content_block_start":
                block = event.get("content_block")
                if isinstance(block, Mapping) and block.get("type") == "tool_use":
                    index = _message_block_index(event)
                    calls[index] = {**block, "fragments": []}
                elif isinstance(block, Mapping) and block.get("type") == "text" and block.get("text"):
                    text.append(str(block["text"]))
                    if runtime.exchange.on_content:
                        runtime.exchange.on_content(str(block["text"]))
            elif kind == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, Mapping):
                    if isinstance(delta.get("text"), str):
                        text.append(delta["text"])
                        if runtime.exchange.on_content:
                            runtime.exchange.on_content(delta["text"])
                    if isinstance(delta.get("thinking"), str):
                        reasoning.append(delta["thinking"])
                        if runtime.exchange.on_reasoning:
                            runtime.exchange.on_reasoning(delta["thinking"])
                    if isinstance(delta.get("partial_json"), str):
                        index = _message_block_index(event)
                        if index not in calls:
                            raise ProviderOutputError("Messages tool delta has no matching content block.")
                        calls[index]["fragments"].append(delta["partial_json"])
            elif kind == "content_block_stop":
                stopped_blocks.add(_message_block_index(event))
            elif kind == "message_delta":
                if isinstance(event.get("delta"), Mapping):
                    stop_reason = str(event["delta"].get("stop_reason") or "") or stop_reason
                if isinstance(event.get("usage"), Mapping):
                    usage.update(event["usage"])
            elif kind == "message_start" and isinstance(event.get("message"), Mapping):
                header = dict(event["message"])
                if isinstance(event["message"].get("usage"), Mapping):
                    usage.update(event["message"]["usage"])
            elif kind == "message_stop":
                stopped = True
            elif kind == "error":
                error = event.get("error")
                detail = error.get("message") if isinstance(error, Mapping) else None
                raise ModelResponseError(
                    str(detail or "Messages stream returned an error."), diagnostics={"provider_error": dict(event)}
                )
        if not stopped or stop_reason is None:
            raise ModelResponseError("Messages stream ended without message_stop and a stop reason.")
        if stop_reason in {"refusal", "content_filter"}:
            raise ModelResponseError(
                f"Messages generation did not complete: {stop_reason}.",
                diagnostics={"finish_reason": stop_reason, "usage": usage},
            )
        blocks = [
            {"type": "text", "text": "".join(text)},
            {"type": "thinking", "thinking": "".join(reasoning)},
        ]
        for index, value in sorted(calls.items()):
            if index not in stopped_blocks:
                if stop_reason == "max_tokens":
                    continue
                raise ProviderOutputError("Messages tool block ended without content_block_stop.")
            fragments = value.pop("fragments")
            blocks.append({**value, "input": "".join(fragments) if fragments else value.get("input")})
        return self._parse_json({**header, "content": blocks, "usage": usage or None, "stop_reason": stop_reason})
