"""Account-scoped Windows audit policy, using native APIs rather than auditpol output."""

from __future__ import annotations

import ctypes
import uuid
from contextlib import contextmanager
from ctypes import wintypes

from ..errors import SandboxInitializationError


class SandboxAuditUnavailable(SandboxInitializationError):
    def __init__(self, message: str = "Windows sandbox permission auditing is unavailable.") -> None:
        super().__init__(message)


class _Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def parse(cls, value: str) -> _Guid:
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class _Policy(ctypes.Structure):
    _fields_ = [("subcategory", _Guid), ("flags", wintypes.ULONG), ("category", _Guid)]


_FILE_SYSTEM = "0cce921d-69ae-11d9-bed3-505054503030"
_REGISTRY = "0cce921e-69ae-11d9-bed3-505054503030"


@contextmanager
def security_privilege():
    import win32api
    import win32event
    import win32security

    mutex = win32event.CreateMutex(None, False, "Global\\PraxisSandboxAuditPolicy")
    acquired = False
    token = None
    previous = None
    try:
        result = win32event.WaitForSingleObject(mutex, 10000)
        if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise SandboxAuditUnavailable("Windows audit policy is busy.")
        acquired = True
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_ADJUST_PRIVILEGES | win32security.TOKEN_QUERY
        )
        privilege = win32security.LookupPrivilegeValue(None, "SeSecurityPrivilege")
        previous = win32security.AdjustTokenPrivileges(token, False, [(privilege, win32security.SE_PRIVILEGE_ENABLED)])
        if win32api.GetLastError():
            raise SandboxAuditUnavailable("Windows permission auditing requires SeSecurityPrivilege.")
        yield
    finally:
        if previous is not None:
            win32security.AdjustTokenPrivileges(token, False, previous)
        if token is not None:
            token.Close()
        if acquired:
            win32event.ReleaseMutex(mutex)
        mutex.Close()


class WindowsAuditPolicy:
    def __init__(self) -> None:
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        self.api.AuditQueryPerUserPolicy.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_Guid),
            wintypes.ULONG,
            ctypes.POINTER(ctypes.POINTER(_Policy)),
        ]
        self.api.AuditSetPerUserPolicy.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Policy), wintypes.ULONG]
        self.api.AuditQuerySystemPolicy.argtypes = [
            ctypes.POINTER(_Guid),
            wintypes.ULONG,
            ctypes.POINTER(ctypes.POINTER(_Policy)),
        ]
        self.api.AuditSetSystemPolicy.argtypes = [ctypes.POINTER(_Policy), wintypes.ULONG]
        self.api.AuditQueryGlobalSaclW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        self.api.AuditSetGlobalSaclW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        self.api.AuditFree.argtypes = [ctypes.c_void_p]
        self.api.AuditFree.restype = None
        for name in (
            "AuditQueryPerUserPolicy",
            "AuditSetPerUserPolicy",
            "AuditQueryGlobalSaclW",
            "AuditSetGlobalSaclW",
            "AuditQuerySystemPolicy",
            "AuditSetSystemPolicy",
        ):
            getattr(self.api, name).restype = ctypes.c_ubyte
        self.api.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
        self.api.AddAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self.api.AddAuditAccessAceEx.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
        ]

    @staticmethod
    def _check(ok: object) -> None:
        if not ok:
            raise SandboxAuditUnavailable(f"Windows audit API failed (code {ctypes.get_last_error()}).")

    def ensure(self, account_sid: str) -> None:
        import win32security

        sid = win32security.ConvertStringSidToSid(account_sid)
        raw_sid = ctypes.create_string_buffer(bytes(sid))
        with security_privilege():
            self._ensure_event_categories()
            self._ensure_system_policy()
            self._ensure_user_policy(raw_sid)
            self._ensure_global_sacl("File", raw_sid, 0x001F01FF)
            self._ensure_global_sacl("Key", raw_sid, 0x000F003F)

    def _ensure_event_categories(self) -> None:
        import win32security

        handle = win32security.LsaOpenPolicy(
            None, win32security.POLICY_VIEW_AUDIT_INFORMATION | win32security.POLICY_SET_AUDIT_REQUIREMENTS
        )
        try:
            enabled, previous = win32security.LsaQueryInformationPolicy(
                handle, win32security.PolicyAuditEventsInformation
            )
            updated = list(previous)
            # Enable object failures, process creation and logoff records without disabling existing audits.
            for category, flags in ((2, 2), (7, 1), (1, 1)):
                updated[category] = (updated[category] & ~4) | flags
            if not enabled or tuple(updated) != previous:
                win32security.LsaSetInformationPolicy(
                    handle, win32security.PolicyAuditEventsInformation, (True, tuple(updated))
                )
            enabled, actual = win32security.LsaQueryInformationPolicy(
                handle, win32security.PolicyAuditEventsInformation
            )
            if not enabled or any(actual[category] & flags != flags for category, flags in ((2, 2), (7, 1), (1, 1))):
                raise SandboxAuditUnavailable("Required Windows audit categories are not enabled.")
        finally:
            handle.Close()

    def _ensure_user_policy(self, sid) -> None:
        guids = (_Guid * 2)(_Guid.parse(_FILE_SYSTEM), _Guid.parse(_REGISTRY))
        existing = ctypes.POINTER(_Policy)()
        self._check(self.api.AuditQueryPerUserPolicy(sid, guids, 2, ctypes.byref(existing)))
        try:
            updated = (_Policy * 2)()
            changed = False
            for index in range(2):
                updated[index].subcategory = guids[index]
                # Include failures; preserve success policy. Do not change system-wide auditing.
                previous = existing[index].flags if existing else 0
                updated[index].flags = (previous & ~0x18) | 0x04
                changed |= updated[index].flags != previous
            if changed:
                self._check(self.api.AuditSetPerUserPolicy(sid, updated, 2))
        finally:
            self.api.AuditFree(existing)

    def _ensure_system_policy(self) -> None:
        guids = (_Guid * 6)(
            *(
                _Guid.parse(value)
                for value in (
                    _FILE_SYSTEM,
                    _REGISTRY,
                    "0cce922b-69ae-11d9-bed3-505054503030",
                    "0cce9216-69ae-11d9-bed3-505054503030",
                    "0cce9226-69ae-11d9-bed3-505054503030",
                    "0cce922c-69ae-11d9-bed3-505054503030",
                )
            )
        )
        existing = ctypes.POINTER(_Policy)()
        self._check(self.api.AuditQuerySystemPolicy(guids, 6, ctypes.byref(existing)))
        try:
            if not existing:
                raise SandboxAuditUnavailable("Windows returned no system audit policy.")
            changed = False
            for index, flags in enumerate((2, 2, 1, 1, 2, 1)):
                updated = (existing[index].flags & ~4) | flags
                changed |= updated != existing[index].flags
                existing[index].flags = updated
            if changed:
                self._check(self.api.AuditSetSystemPolicy(existing, 6))
        finally:
            self.api.AuditFree(existing)

    def _ensure_global_sacl(self, object_type: str, sid, mask: int) -> None:
        pointer = ctypes.c_void_p()
        self._check(self.api.AuditQueryGlobalSaclW(object_type, ctypes.byref(pointer)))
        try:
            old = (
                ctypes.string_at(pointer, ctypes.c_ushort.from_address(pointer.value + 2).value)
                if pointer.value
                else b""
            )
            count = int.from_bytes(old[4:6], "little") if old else 0
            offset = 8
            sid_bytes = bytes(sid)[:-1]
            for _ in range(count):
                size = int.from_bytes(old[offset + 2 : offset + 4], "little")
                ace = old[offset : offset + size]
                if (
                    ace[0] == 2
                    and ace[1] & 0x80
                    and ace[8:] == sid_bytes
                    and int.from_bytes(ace[4:8], "little") & mask == mask
                ):
                    return
                offset += size
            # Copy every existing ACE, then add a failure-only ACE for this sandbox account.
            buffer = ctypes.create_string_buffer(max(8, len(old)) + 8 + len(sid_bytes))
            self._check(self.api.InitializeAcl(buffer, len(buffer), 4))
            if count:
                ace_bytes = ctypes.create_string_buffer(old[8:offset])
                self._check(self.api.AddAce(buffer, 4, 0xFFFFFFFF, ace_bytes, offset - 8))
            self._check(self.api.AddAuditAccessAceEx(buffer, 4, 0, mask, sid, False, True))
            self._check(self.api.AuditSetGlobalSaclW(object_type, buffer))
        finally:
            if pointer.value:
                self.api.AuditFree(pointer)
