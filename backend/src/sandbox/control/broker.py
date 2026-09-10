"""Authenticated client for the standalone Windows Sandbox Broker."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from base64 import b64decode, b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.domain import error_report, normalize_error_report, redact_sensitive_text, safe_error_message

from ..errors import (
    BrokerInstallationError,
    BrokerInstallFailureCode,
    BrokerStatusFailureCode,
    SandboxCleanupPending,
    SandboxError,
    SandboxFailureCode,
    SandboxInitializationError,
    SandboxPolicyError,
    SandboxResourceExceeded,
)

_PROCESS_BACKEND_INSTANCE_ID = f"backend-{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class BrokerStatus:
    installed: bool
    healthy: bool
    code: BrokerStatusFailureCode | None = None
    version: str | None = None
    installation_id: str | None = None
    detail: str | None = None
    generation: str | None = None
    proxy_port: int | None = None
    token_model: str | None = None
    error_report: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "healthy": self.healthy,
            "code": self.code.value if self.code is not None else None,
            "version": self.version,
            "installation_id": self.installation_id,
            "detail": self.detail,
            "generation": self.generation,
            "proxy_port": self.proxy_port,
            "token_model": self.token_model,
            "error_report": self.error_report,
        }


class WindowsBrokerClient:
    """Small protocol client; all privileged operations stay in the Broker."""

    def __init__(
        self,
        *,
        pipe_name: str = r"\\.\pipe\praxis-sandbox-broker",
        installation_key: bytes | None = None,
        transport: Callable[[bytes], bytes] | None = None,
        is_windows: bool | None = None,
        backend_instance_id: str | None = None,
        key_store: Any | None = None,
        installer: Any | None = None,
        clock: Callable[[], float] | None = None,
        request_ttl_seconds: int = 30,
        ready_path: Path | None = None,
        expected_proxy_port: int = 17831,
    ) -> None:
        self.pipe_name = pipe_name
        self._initialization_error: Exception | None = None
        self._key = installation_key
        self._transport = transport
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self.backend_instance_id = backend_instance_id or _PROCESS_BACKEND_INSTANCE_ID
        self._key_store = key_store
        self._installer = installer
        self._clock = clock or time.time
        if not 1 <= request_ttl_seconds <= 60:
            raise ValueError("request_ttl_seconds must be between 1 and 60")
        self._request_ttl_seconds = request_ttl_seconds
        self._seen_nonces: set[str] = set()
        self._ready_path = Path(ready_path) if ready_path is not None else None
        self._expected_proxy_port = expected_proxy_port

    @classmethod
    def from_system(
        cls,
        *,
        is_windows: bool | None = None,
        expected_proxy_port: int = 17831,
    ) -> WindowsBrokerClient:
        """Load the locally installed DPAPI key without creating one.

        Installation is an explicit control-plane action.  A missing key is
        therefore represented by an unavailable client and never replaced by a
        plaintext or newly generated key during normal command execution.
        """

        resolved_windows = os.name == "nt" if is_windows is None else is_windows
        if not resolved_windows:
            return cls(is_windows=False)
        try:
            from ..broker_service import BrokerConfiguration, DpapiKeyStore, WindowsServiceInstaller
            from ..broker_service.installer import _absolute_windows_path
        except Exception as exc:
            client = cls(is_windows=True)
            client._initialization_error = exc
            return client
        configuration = BrokerConfiguration.create()
        key_store = DpapiKeyStore(configuration.installation_key_path)
        source_root = Path(_absolute_windows_path(Path(__file__).resolve().parents[2]))
        source_boundary = Path(_absolute_windows_path(Path(__file__).resolve().parents[4]))
        runtime_prefix = Path(_absolute_windows_path(sys.prefix))
        runtime_base_prefix = Path(_absolute_windows_path(sys.base_prefix))
        installer = WindowsServiceInstaller(
            (_absolute_windows_path(Path(sys.prefix) / "pythonservice.exe"),),
            service_class=(rf"{source_root}\sandbox_service_bootstrap.PraxisSandboxBrokerService"),
            backend_sid_path=configuration.backend_sid_path,
            program_data_path=configuration.program_data,
            service_code_path=source_root,
            service_code_boundary_path=source_boundary,
            service_runtime_paths=(runtime_prefix, runtime_base_prefix),
            proxy_port=expected_proxy_port,
        )
        initialization_error = None
        try:
            key = key_store.load()
        except Exception as exc:
            initialization_error = exc
            key = None
        client = cls(
            pipe_name=configuration.pipe_name,
            installation_key=key,
            is_windows=True,
            key_store=key_store,
            installer=installer,
            ready_path=configuration.ready_path,
            expected_proxy_port=expected_proxy_port,
        )
        client._initialization_error = initialization_error
        return client

    @property
    def available(self) -> bool:
        return self._transport is not None or self._is_windows

    def status(self) -> BrokerStatus:
        if not self.available:
            return BrokerStatus(
                False,
                False,
                code=BrokerStatusFailureCode.UNAVAILABLE,
                detail="Windows Broker is unavailable",
            )
        installed = True
        failure_code = BrokerStatusFailureCode.STATUS_FAILED
        try:
            service_installed = getattr(self._installer, "service_installed", None)
            if callable(service_installed):
                installed = bool(service_installed())
                if not installed:
                    return BrokerStatus(
                        False,
                        False,
                        code=BrokerStatusFailureCode.NOT_INSTALLED,
                        detail="Windows Broker is not installed",
                    )
            if self._initialization_error is not None:
                return BrokerStatus(
                    True,
                    False,
                    code=BrokerStatusFailureCode.INSTALLATION_KEY_MISSING,
                    detail=safe_error_message(self._initialization_error),
                    error_report=error_report(self._initialization_error),
                )
            failure_code = BrokerStatusFailureCode.SERVICE_CONFIGURATION_INVALID
            configuration_healthy = getattr(self._installer, "configuration_healthy", None)
            if callable(configuration_healthy) and not configuration_healthy():
                raise SandboxInitializationError("Broker service configuration requires repair")
            failure_code = BrokerStatusFailureCode.READY_MARKER_INVALID
            if self._ready_path is not None:
                from ..broker_service import read_ready_marker

                marker = read_ready_marker(self._ready_path, expected_proxy_port=self._expected_proxy_port)
            else:
                marker = {}
            failure_code = BrokerStatusFailureCode.STATUS_FAILED
            payload = self.request("status", {})
            failure_code = BrokerStatusFailureCode.PROTOCOL_INCOMPATIBLE
            if payload.get("version") != "3":
                raise SandboxInitializationError("Broker protocol version requires repair")
            failure_code = BrokerStatusFailureCode.TOKEN_MODEL_INCOMPATIBLE
            if payload.get("token_model") != "capability_sid_v3":
                raise SandboxInitializationError("Broker token model requires repair")
            failure_code = BrokerStatusFailureCode.GENERATION_MISMATCH
            if marker and payload.get("generation") != marker.get("generation"):
                raise SandboxInitializationError("Broker generation requires repair")
            healthy = bool(payload.get("healthy"))
            payload_installed = bool(payload.get("installed"))
            resolved_installed = installed if self._installer is not None else payload_installed
            detail = str(payload.get("detail")) if payload.get("detail") else None
            return BrokerStatus(
                resolved_installed,
                healthy,
                code=None if healthy else BrokerStatusFailureCode.UNHEALTHY,
                version=str(payload.get("version")) if payload.get("version") else None,
                installation_id=(str(payload.get("installation_id")) if payload.get("installation_id") else None),
                detail=None if healthy else detail or "Broker reported unhealthy status",
                generation=str(payload.get("generation")) if payload.get("generation") else None,
                proxy_port=int(payload["proxy_port"]) if isinstance(payload.get("proxy_port"), int) else None,
                token_model=str(payload.get("token_model")) if payload.get("token_model") else None,
            )
        except Exception as exc:
            detail = safe_error_message(exc)
            return BrokerStatus(
                installed,
                False,
                code=getattr(exc, "broker_status_code", None)
                or (
                    BrokerStatusFailureCode.PIPE_UNAVAILABLE
                    if failure_code == BrokerStatusFailureCode.STATUS_FAILED and isinstance(exc, OSError)
                    else failure_code
                ),
                error_report=error_report(exc),
                detail=detail,
            )

    def install(self) -> BrokerStatus:
        current = self.status()
        if current.healthy:
            return current
        if self._installer is None:
            self._ensure_installation_key()
        return self._status_command("repair" if self._installer is not None else "install")

    def repair(self) -> BrokerStatus:
        if self._installer is None:
            self._ensure_installation_key()
        return self._status_command("repair")

    def _status_command(self, operation: str) -> BrokerStatus:
        if self._installer is not None:
            getattr(self._installer, operation)()
            self._initialization_error = None
            if self._key_store is not None:
                self._key = None
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    if self._key is None and self._key_store is not None:
                        self._key = self._key_store.load()
                    status = self.status()
                    if status.healthy:
                        return status
                    failure = SandboxInitializationError(status.detail or "Broker is not healthy")
                    failure.error_report = status.error_report
                    raise failure
                except Exception as exc:
                    if time.monotonic() >= deadline:
                        failure = BrokerInstallationError(BrokerInstallFailureCode.NOT_READY, safe_error_message(exc))
                        failure.error_report = error_report(exc)
                        raise failure from None
                    time.sleep(0.1)
        payload = self.request(operation, {})
        healthy = bool(payload.get("healthy", True))
        return BrokerStatus(
            bool(payload.get("installed", True)),
            healthy,
            code=None if healthy else BrokerStatusFailureCode.UNHEALTHY,
            detail=(
                str(payload.get("detail"))
                if not healthy and payload.get("detail")
                else ("Broker reported unhealthy status" if not healthy else None)
            ),
        )

    def launch(
        self,
        *,
        argv: list[str],
        cwd: str,
        environment: Mapping[str, str],
        reservation_id: str,
        policy_hash: str,
        capability_digest: str,
        user_id: str,
    ) -> Any:
        response = self.request(
            "launch",
            {
                "argv": list(argv),
                "cwd": cwd,
                "environment": dict(environment),
                "reservation_id": reservation_id,
                "policy_hash": policy_hash,
                "capability_digest": capability_digest,
                "backend_instance_id": self.backend_instance_id,
                "user_id": user_id,
            },
        )
        if not response.get("accepted", False):
            raise SandboxInitializationError("Windows Broker rejected sandbox launch")
        # The real Broker returns a process handle/identifier. A development
        # client may return a Popen-compatible object for tests.
        process = response.get("process")
        if process is not None:
            return process
        process_id = response.get("process_id")
        pid = response.get("pid")
        if not isinstance(process_id, str) or not process_id or isinstance(pid, bool) or not isinstance(pid, int):
            raise SandboxInitializationError("Windows Broker did not return a process handle")
        return BrokerManagedProcess(
            self,
            process_id,
            pid,
            stdin_enabled=response.get("stdin") == "pipe",
            stdout_enabled=response.get("stdout") == "pipe",
            stderr_enabled=response.get("stderr") == "pipe",
        )

    def reserve(self, *, policy: Mapping[str, Any], policy_hash: str, user_id: str) -> dict[str, Any]:
        response = self.request(
            "reserve",
            {
                "policy": dict(policy),
                "policy_hash": policy_hash,
                "backend_instance_id": self.backend_instance_id,
                "user_id": user_id,
            },
        )
        reservation_id = response.get("reservation_id")
        logon_sid = response.get("logon_sid")
        capability_sids = response.get("capability_sids")
        capability_digest = response.get("capability_digest")
        if (
            not response.get("reserved")
            or not isinstance(reservation_id, str)
            or not isinstance(logon_sid, str)
            or not isinstance(capability_sids, Mapping)
            or not isinstance(capability_sids.get("workspace"), str)
            or not isinstance(capability_sids.get("temp"), str)
            or not isinstance(capability_digest, str)
            or not capability_digest
        ):
            raise SandboxInitializationError("Windows Broker did not return a reservation")
        return response

    def reclaim_stale(self) -> tuple[str, ...]:
        response = self.request("reclaim", {"backend_instance_id": self.backend_instance_id})
        raw = response.get("reclaimed")
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise SandboxInitializationError("Windows Broker returned invalid reclaim data")
        return tuple(raw)

    def release(self, job_id: str, *, user_id: str) -> None:
        """Drop the Broker-side resource lease for one completed Job."""

        response = self.request(
            "release",
            {"backend_instance_id": self.backend_instance_id, "user_id": user_id, "job_id": job_id},
        )
        if not response.get("released", False):
            raise SandboxInitializationError("Windows Broker did not release the Job")

    def request(self, operation: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.available:
            raise SandboxInitializationError("Windows Broker is not installed")
        nonce = secrets.token_urlsafe(24)
        if nonce in self._seen_nonces:
            raise SandboxInitializationError("Broker nonce collision")
        self._seen_nonces.add(nonce)
        issued_at = int(self._clock())
        envelope = {
            "operation": operation,
            "nonce": nonce,
            "issued_at": issued_at,
            "expires_at": issued_at + self._request_ttl_seconds,
            "body": dict(body),
        }
        encoded = _canonical(envelope)
        envelope["hmac"] = self._sign(encoded)
        response_bytes = self._send(_canonical(envelope))
        try:
            response = json.loads(response_bytes.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            exc.broker_status_code = BrokerStatusFailureCode.RESPONSE_INVALID
            raise
        if not isinstance(response, dict) or response.get("nonce") != nonce:
            raise SandboxInitializationError(
                "Windows Broker response replay detected", status_code=BrokerStatusFailureCode.RESPONSE_INVALID
            )
        response_hmac = response.pop("hmac", None)
        if not isinstance(response_hmac, str) or not hmac.compare_digest(
            response_hmac, self._sign(_canonical(response))
        ):
            raise SandboxInitializationError(
                "Windows Broker response authentication failed",
                status_code=BrokerStatusFailureCode.RESPONSE_AUTHENTICATION_FAILED,
            )
        error = response.get("error")
        if error is not None:
            self._raise_remote_error(error)
        return response

    @staticmethod
    def _raise_remote_error(value: object) -> None:
        if not isinstance(value, Mapping):
            raise SandboxInitializationError("Windows Broker returned an invalid error")
        raw_code = value.get("code")
        code = SandboxFailureCode(str(raw_code))
        message = redact_sensitive_text(str(value.get("message") or "Windows Broker operation failed"))
        errors: dict[SandboxFailureCode, type[SandboxError]] = {
            SandboxFailureCode.INIT_FAILED: SandboxInitializationError,
            SandboxFailureCode.POLICY_FAILED: SandboxPolicyError,
            SandboxFailureCode.RESOURCE_EXCEEDED: SandboxResourceExceeded,
            SandboxFailureCode.CLEANUP_PENDING: SandboxCleanupPending,
        }
        error_type = errors.get(code)
        if error_type is not None:
            failure = error_type(message)
            failure.error_report = normalize_error_report(value.get("error_report"))
            raise failure
        failure = SandboxError(message, code)
        failure.error_report = normalize_error_report(value.get("error_report"))
        raise failure

    def _send(self, payload: bytes) -> bytes:
        if self._transport is not None:
            return self._transport(payload)
        if not self._is_windows:
            raise SandboxInitializationError("Windows Broker is unavailable")
        with open(self.pipe_name, "r+b", buffering=0) as pipe:
            pipe.write(payload)
            return pipe.read(1024 * 1024)

    def _sign(self, payload: bytes) -> str:
        if self._key is None:
            raise SandboxInitializationError(
                "Broker installation key is missing", status_code=BrokerStatusFailureCode.INSTALLATION_KEY_MISSING
            )
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()


class _BrokerReadStream:
    def __init__(self, process: BrokerManagedProcess, stream: str) -> None:
        self._process = process
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        response = self._process._control(
            "process_read",
            {"stream": self._stream, "size": 65536 if size is None or size < 0 else size},
        )
        payload = response.get("data")
        if not isinstance(payload, str):
            raise OSError("Broker process stream returned invalid data")
        return b64decode(payload.encode("ascii"))

    def close(self) -> None:
        return None


class _BrokerWriteStream:
    def __init__(self, process: BrokerManagedProcess) -> None:
        self._process = process

    def write(self, value: bytes) -> int:
        response = self._process._control(
            "process_write",
            {"data": b64encode(bytes(value)).decode("ascii")},
        )
        return int(response.get("written", len(value)))

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self._process._control("process_close_stdin", {})


class BrokerManagedProcess:
    """Popen-compatible proxy for a process owned by the Broker service."""

    def __init__(
        self,
        client: WindowsBrokerClient,
        process_id: str,
        pid: int,
        *,
        stdin_enabled: bool,
        stdout_enabled: bool,
        stderr_enabled: bool,
    ) -> None:
        self._client = client
        self._process_id = process_id
        self.pid = pid
        self.returncode: int | None = None
        self.stdin = _BrokerWriteStream(self) if stdin_enabled else None
        self.stdout = _BrokerReadStream(self, "stdout") if stdout_enabled else None
        self.stderr = _BrokerReadStream(self, "stderr") if stderr_enabled else None

    def _control(self, operation: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return self._client.request(
            operation,
            {
                "process_id": self._process_id,
                "backend_instance_id": self._client.backend_instance_id,
                **dict(values),
            },
        )

    def poll(self) -> int | None:
        response = self._control("process_poll", {})
        return self._capture_returncode(response)

    def wait(self, timeout: float | None = None) -> int:
        response = self._control("process_wait", {"timeout": timeout})
        code = self._capture_returncode(response)
        if code is None:
            raise subprocess.TimeoutExpired(["sandbox-process"], timeout)
        return code

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes | None, bytes | None]:
        response = self._control(
            "process_communicate",
            {
                "input": b64encode(input).decode("ascii") if input is not None else None,
                "timeout": timeout,
            },
        )
        code = self._capture_returncode(response)
        if code is None:
            raise subprocess.TimeoutExpired(["sandbox-process"], timeout)
        stdout = response.get("stdout")
        stderr = response.get("stderr")
        return (
            b64decode(stdout.encode("ascii")) if isinstance(stdout, str) else None,
            b64decode(stderr.encode("ascii")) if isinstance(stderr, str) else None,
        )

    def terminate(self) -> None:
        self._capture_returncode(self._control("process_terminate", {}))

    def kill(self) -> None:
        self._capture_returncode(self._control("process_kill", {}))

    def _capture_returncode(self, response: Mapping[str, Any]) -> int | None:
        value = response.get("returncode")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise OSError("Broker process returned an invalid exit code")
        self.returncode = value
        return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


__all__ = ["BrokerManagedProcess", "BrokerStatus", "WindowsBrokerClient"]
