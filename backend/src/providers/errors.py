"""Provider-specific failures, separate from UI and runtime implementations."""

from collections.abc import Mapping
from typing import Any

from backend.domain import ModelOutputError, PlanningError


class ModelConfigurationError(ValueError):
    """The local model configuration is incomplete."""


class ModelRequestError(PlanningError):
    """The model endpoint could not provide a usable response."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message, diagnostics=diagnostics)


class ModelTransportError(ModelRequestError):
    """A transport failure with enough metadata to apply a retry policy."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        retry_after: float | None = None,
        stream_started: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.stream_started = stream_started


class ModelResponseError(ModelRequestError):
    """A provider failure that must not enter output repair."""

    @property
    def is_connection_interruption(self) -> bool:
        codes = {
            "upstream_stream_read_error",
            "upstream_http2_stream_error",
            "upstream_stream_truncated",
        }
        error = self.diagnostics.get("error")
        provider_error = self.diagnostics.get("provider_error")
        nested_error = provider_error.get("error") if isinstance(provider_error, Mapping) else None
        return any(
            isinstance(value, str) and value in codes
            for detail in (error, provider_error, nested_error)
            if isinstance(detail, Mapping)
            for value in (detail.get("code"), detail.get("type"))
        )


class ProviderOutputError(ModelOutputError, ModelRequestError):
    """Provider response validation failed while preserving request-error compatibility."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        invalid_output: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        ModelOutputError.__init__(
            self,
            message,
            operation=operation,
            invalid_output=invalid_output,
            diagnostics=diagnostics,
        )
