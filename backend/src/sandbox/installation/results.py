"""One protected, request-scoped diagnostic result from the elevated helper."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from backend.domain import error_report, normalize_error_report

from .access_policy import _is_reparse_point


def _remove_result(path: Path) -> None:
    original = sys.exception()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        if original is None:
            raise
        logging.getLogger(__name__).warning("Repair result cleanup failed: %s", error_report(exc))


def result_path(directory: Path, request_id: str) -> Path:
    if re.fullmatch(r"[a-f0-9]{32}", request_id) is None:
        raise ValueError("Invalid repair result request id")
    directory = Path(directory).absolute()
    if directory.name != "SandboxBroker" or directory.parent.name != "Praxis":
        raise ValueError("Repair result is outside the Broker directory")
    target = directory / f"repair-{request_id}.json"
    for path in (target, directory, *directory.parents):
        try:
            linked = _is_reparse_point(path)
        except FileNotFoundError:
            continue
        if linked:
            raise ValueError("Repair result path contains a reparse point")
    return target


def write_result(directory: Path, request_id: str, error: BaseException, backend_sid: str | None = None) -> None:
    target = result_path(directory, request_id)
    # The helper never creates or changes the installation directory for diagnostics.
    if not target.parent.is_dir():
        return
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(error_report(error), ensure_ascii=True).encode("ascii")
    try:
        if os.name == "nt":
            import pywintypes
            import win32con
            import win32file
            import win32security

            sid_ace = ""
            if backend_sid:
                sid = win32security.ConvertStringSidToSid(backend_sid)
                sid_ace = f"(A;;FRSD;;;{win32security.ConvertSidToStringSid(sid)})"
            attributes = pywintypes.SECURITY_ATTRIBUTES()
            attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                "D:P(A;;FA;;;SY)(A;;FA;;;BA)" + sid_ace, win32security.SDDL_REVISION_1
            )
            handle = win32file.CreateFile(
                str(temporary),
                win32con.GENERIC_WRITE,
                0,
                attributes,
                win32con.CREATE_NEW,
                win32con.FILE_ATTRIBUTE_NORMAL,
                None,
            )
            try:
                win32file.WriteFile(handle, data)
            finally:
                handle.Close()
        else:
            with temporary.open("xb") as stream:
                stream.write(data)
        result_path(directory, request_id)
        os.replace(temporary, target)
    finally:
        _remove_result(temporary)


def read_result(directory: Path, request_id: str) -> dict[str, Any] | None:
    target = result_path(directory, request_id)
    try:
        with target.open("rb") as stream:
            raw = stream.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError("Repair result exceeds its output limit")
        report = normalize_error_report(json.loads(raw))
        if report is None:
            raise ValueError("Repair result does not contain a valid error report")
        return report
    except FileNotFoundError:
        return None
    finally:
        _remove_result(target)
