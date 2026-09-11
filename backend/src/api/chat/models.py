"""Validated request model and provider-neutral request parameters."""

from __future__ import annotations

from backend.domain.execution_config import ReasoningEffort, RuntimeModelRequest
from backend.providers import ModelConfig, ModelConfigurationError
from backend.storage.settings.crypto import SecretDecryptionError

from ..state import WebAppState


def _reasoning_parameters(effort: ReasoningEffort) -> dict[str, object]:
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


def _model_request_parameters(model: RuntimeModelRequest | None, fallback: ReasoningEffort) -> dict[str, object]:
    """Translate the complete public model object into provider request options."""

    if model is None:
        return _reasoning_parameters(fallback)
    if model.thinking == "disable":
        return {
            "thinking": {"type": "disabled"},
            "max_tokens": model.output_length,
            "temperature": model.temperature,
        }
    return {
        "thinking": {"type": "enabled"},
        "reasoning_effort": model.reasoning_effort,
        "max_tokens": model.output_length,
        "temperature": model.temperature,
    }


def _model_config_snapshot(
    state: WebAppState,
    *,
    provider_name: str | None = None,
) -> ModelConfig:
    try:
        if provider_name and provider_name != "unknown":
            return state.model_config(provider_name)
        return state.model_config()
    except SecretDecryptionError as exc:
        exc.api_status_code = 409
        raise
    except ModelConfigurationError as exc:
        exc.api_status_code = 422
        raise
