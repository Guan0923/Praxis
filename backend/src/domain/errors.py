"""Shared errors and safe user-visible error projection."""

import re
from collections.abc import Mapping
from typing import Any

_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(api[\s_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|cookie|password|secret|token)\b"
    r"[\"']?\s*([=:])\s*[\"']?([^\r\n,;&\"'}]+)"
)
_PRIVATE_HEADER = re.compile(r"(?im)\b(cookie|set-cookie|authorization|proxy-authorization)[\"']?\s*([:=])\s*[^\r\n]+")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^\s/@]+@")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
_SECRET_KEY = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}\b")
_REPORT_LIMIT = 64 * 1024


def _bounded(value: str) -> str:
    return value if len(value) <= _REPORT_LIMIT else "[truncated]\n" + value[-_REPORT_LIMIT:]


def root_error(error: BaseException) -> BaseException:
    """Return the deepest active explicit or implicit cause without looping forever."""

    current = error
    seen = {id(current)}
    while True:
        candidate = current.__cause__
        if candidate is None and not current.__suppress_context__:
            candidate = current.__context__
        if candidate is None or id(candidate) in seen:
            return current
        seen.add(id(candidate))
        current = candidate


def redact_sensitive_text(value: str) -> str:
    """Redact credential-shaped values from one externally visible string."""

    value = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
    value = _PRIVATE_HEADER.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _SECRET_KEY.sub("[REDACTED]", value)
    return _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)


def normalize_error_report(value: object) -> dict[str, Any] | None:
    """Accept only diagnostic fields, including across untrusted process boundaries."""
    if not isinstance(value, Mapping):
        return None
    if not all(isinstance(value.get(key), str) for key in ("type", "message", "traceback")):
        return None
    report = {key: _bounded(redact_sensitive_text(value[key])) for key in ("type", "message", "traceback")}
    for key in ("errno", "winerror"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool):
            report[key] = number
    return report


def error_report(error: BaseException) -> dict[str, Any]:
    """Capture origin frames without source lines, locals, or request objects."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not item for item in chain):
        remote = normalize_error_report(getattr(current, "error_report", None))
        if remote is not None:
            return remote
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    root = chain[-1]
    lines: list[str] = []
    for item in chain:
        if lines:
            lines.append("Caused by:")
        lines.append(f"{type(item).__name__}: {item}")
        lines.extend(str(note) for note in getattr(item, "__notes__", ()))
        tb = item.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            lines.append(f'  File "{code.co_filename}", line {tb.tb_lineno}, in {code.co_name}')
            tb = tb.tb_next
        if isinstance(item, BaseExceptionGroup):
            for child in item.exceptions:
                lines.append(error_report(child)["traceback"])
    report = normalize_error_report(
        {
            "type": type(root).__name__,
            "message": str(root) or type(root).__name__,
            "traceback": "\n".join(lines),
            "errno": getattr(root, "errno", None),
            "winerror": getattr(root, "winerror", None),
        }
    )
    return report or {"type": type(root).__name__, "message": "", "traceback": ""}


def safe_error_message(error: BaseException) -> str:
    """Project one exception chain to its redacted, unwrapped root message."""

    return error_report(error)["message"]


class PlanningError(RuntimeError):
    """A planner could not produce a valid decision or execution plan."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class TracePersistenceError(RuntimeError):
    """A required Turn audit snapshot could not be saved before transport."""


class ModelOutputError(PlanningError):
    """A model response was received but did not satisfy the required contract."""

    _MAX_PREVIEW_CHARS = 2_000

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        invalid_output: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.operation = operation
        self.validation_error = message
        self.invalid_output_preview = (invalid_output or "")[: self._MAX_PREVIEW_CHARS]
