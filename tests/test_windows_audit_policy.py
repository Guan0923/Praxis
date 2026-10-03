from __future__ import annotations

import ctypes
import os
import struct
from contextlib import nullcontext

import pytest

from backend.sandbox.native_windows import audit_policy
from backend.sandbox.native_windows.audit_policy import SandboxAuditUnavailable, WindowsAuditPolicy, _Policy


class FakeAuditApi:
    """In-memory API double: never reads or changes Windows audit policy."""

    def __init__(self, flags: list[int] | None) -> None:
        self.flags = flags
        self.last_error = 0
        self.query_calls = 0
        self.set_calls = 0
        self.query_error: int | None = None
        self.verify_error: int | None = None
        self.set_error: int | None = None
        self.null_query: int | None = None
        self.ignore_update = False
        self.buffers: list = []
        self.freed: list[int] = []

    def AuditQueryPerUserPolicy(self, _sid, guids, count, output) -> bool:
        self.query_calls += 1
        error = self.query_error if self.query_calls == 1 else self.verify_error
        if error is not None or self.flags is None:
            self.last_error = error if error is not None else 2
            return False
        if self.query_calls == self.null_query:
            return True
        buffer = (_Policy * count)()
        for index in range(count):
            buffer[index].subcategory = guids[index]
            buffer[index].flags = self.flags[index]
        self.buffers.append(buffer)
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(_Policy)))[0] = ctypes.cast(buffer, ctypes.POINTER(_Policy))
        return True

    def AuditSetPerUserPolicy(self, _sid, updated, count) -> bool:
        self.set_calls += 1
        if self.set_error is not None:
            self.last_error = self.set_error
            return False
        if not self.ignore_update:
            self.flags = [updated[index].flags for index in range(count)]
        return True

    def AuditFree(self, pointer) -> None:
        address = ctypes.cast(pointer, ctypes.c_void_p).value
        assert address is not None
        assert address not in self.freed
        self.freed.append(address)


@pytest.fixture
def make_policy(monkeypatch):
    def create(api: FakeAuditApi) -> WindowsAuditPolicy:
        monkeypatch.setattr(audit_policy.ctypes, "get_last_error", lambda: api.last_error, raising=False)
        policy = WindowsAuditPolicy.__new__(WindowsAuditPolicy)
        policy.api = api
        if hasattr(api, "LocalFree"):
            policy.kernel = api
        return policy

    return create


def test_missing_user_policy_is_created_and_read_back(make_policy) -> None:
    api = FakeAuditApi(None)
    make_policy(api)._ensure_user_policy(None)

    assert api.flags == [0x04, 0x04]
    assert api.query_calls == 2
    assert api.set_calls == 1
    assert len(api.freed) == 1


@pytest.mark.parametrize("flags", [[0, 0], [0x01 | 0x08, 0x02 | 0x10]])
def test_existing_user_policy_preserves_success_bits_and_enables_failures(make_policy, flags) -> None:
    api = FakeAuditApi(flags)
    make_policy(api)._ensure_user_policy(None)

    assert api.flags == [(flag & ~0x18) | 0x04 for flag in flags]
    assert api.query_calls == 2
    assert api.set_calls == 1
    assert len(api.freed) == 2


def test_already_enabled_policy_needs_no_write(make_policy) -> None:
    api = FakeAuditApi([0x05, 0x04])
    make_policy(api)._ensure_user_policy(None)

    assert api.query_calls == 1
    assert api.set_calls == 0
    assert len(api.freed) == 1


@pytest.mark.parametrize("error", [5, 87, 1314])
def test_other_query_errors_remain_fatal(make_policy, error) -> None:
    api = FakeAuditApi([0, 0])
    api.query_error = error
    with pytest.raises(SandboxAuditUnavailable, match=rf"code {error}\b"):
        make_policy(api)._ensure_user_policy(None)

    assert api.set_calls == 0
    assert api.query_calls == 1
    assert not api.freed


@pytest.mark.parametrize("flags", [None, [0, 0]])
def test_policy_write_failure_is_not_ignored(make_policy, flags) -> None:
    api = FakeAuditApi(flags)
    api.set_error = 5
    with pytest.raises(SandboxAuditUnavailable, match="code 5"):
        make_policy(api)._ensure_user_policy(None)

    assert api.query_calls == 1
    assert api.set_calls == 1
    assert len(api.freed) == (0 if flags is None else 1)


@pytest.mark.parametrize("error", [2, 5])
def test_verification_query_failure_remains_fatal_and_frees_initial_buffer(make_policy, error) -> None:
    api = FakeAuditApi([0, 0])
    api.verify_error = error
    with pytest.raises(SandboxAuditUnavailable, match=rf"code {error}\b"):
        make_policy(api)._ensure_user_policy(None)

    assert api.query_calls == 2
    assert len(api.freed) == 1


def test_successful_write_without_effect_is_rejected(make_policy) -> None:
    api = FakeAuditApi([0, 0])
    api.ignore_update = True
    with pytest.raises(SandboxAuditUnavailable, match="did not take effect"):
        make_policy(api)._ensure_user_policy(None)

    assert len(api.freed) == 2


def test_successful_query_with_null_buffer_is_rejected(make_policy) -> None:
    api = FakeAuditApi([0, 0])
    api.null_query = 1
    with pytest.raises(SandboxAuditUnavailable, match="returned no per-user audit policy"):
        make_policy(api)._ensure_user_policy(None)

    assert api.set_calls == 0
    assert not api.freed


def test_successful_verification_with_null_buffer_is_rejected(make_policy) -> None:
    api = FakeAuditApi([0, 0])
    api.null_query = 2
    with pytest.raises(SandboxAuditUnavailable, match="did not take effect"):
        make_policy(api)._ensure_user_policy(None)

    assert len(api.freed) == 1


_SID = b"\x01\x02\x00\x00\x00\x00\x00\x05" + struct.pack("<II", 32, 545)
_OTHER_SID = b"\x01\x02\x00\x00\x00\x00\x00\x05" + struct.pack("<II", 32, 544)


def audit_ace(sid: bytes, mask: int, flags: int = 0x80) -> bytes:
    return struct.pack("<BBHI", 2, flags, 8 + len(sid), mask) + sid


def global_acl(entries: list[bytes], capacity: int | None = None) -> bytes:
    payload = b"".join(entries)
    size = capacity if capacity is not None else 8 + len(payload)
    assert size >= 8 + len(payload)
    return struct.pack("<BBHHH", 4, 0, size, len(entries), 0) + payload + bytes(size - 8 - len(payload))


class FakeGlobalAuditApi(FakeAuditApi):
    def __init__(self, initial: bytes | None) -> None:
        super().__init__(None)
        self.acls = {"File": initial, "Key": initial}
        self.global_queries: list[str] = []
        self.global_sets: list[str] = []
        self.global_query_errors: dict[int, int] = {}
        self.global_set_error: int | None = None
        self.global_null_query: int | None = None
        self.ignore_global_update = False
        self.local_freed: list[int] = []
        self.acl_entries: dict[int, list[bytes]] = {}

    def AuditQueryGlobalSaclW(self, object_type, output) -> bool:
        self.global_queries.append(object_type)
        error = self.global_query_errors.get(len(self.global_queries))
        raw = self.acls[object_type]
        if error is not None or raw is None:
            self.last_error = error if error is not None else 2
            return False
        if len(self.global_queries) == self.global_null_query:
            return True
        buffer = ctypes.create_string_buffer(raw)
        self.buffers.append(buffer)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
        return True

    def LocalFree(self, pointer) -> None:
        address = ctypes.cast(pointer, ctypes.c_void_p).value
        assert address is not None
        assert address not in self.local_freed
        self.local_freed.append(address)

    def InitializeAcl(self, buffer, size, revision) -> bool:
        assert revision == 4
        self.acl_entries[ctypes.addressof(buffer)] = []
        ctypes.memmove(buffer, global_acl([], size), size)
        return True

    def _append(self, buffer, entries: list[bytes]) -> None:
        current = self.acl_entries[ctypes.addressof(buffer)]
        current.extend(entries)
        ctypes.memmove(buffer, global_acl(current, len(buffer)), len(buffer))

    def AddAce(self, buffer, revision, index, entries, size) -> bool:
        assert revision == 4 and index == 0xFFFFFFFF
        raw = ctypes.string_at(entries, size)
        copied = []
        offset = 0
        while offset < size:
            entry_size = int.from_bytes(raw[offset + 2 : offset + 4], "little")
            assert entry_size >= 4
            copied.append(raw[offset : offset + entry_size])
            offset += entry_size
        assert offset == size
        self._append(buffer, copied)
        return True

    def AddAuditAccessAceEx(self, buffer, revision, flags, mask, sid, success, failure) -> bool:
        assert revision == 4 and flags == 0
        assert success is False and failure is True
        self._append(buffer, [audit_ace(bytes(sid)[:-1], mask)])
        return True

    def AuditSetGlobalSaclW(self, object_type, buffer) -> bool:
        self.global_sets.append(object_type)
        if self.global_set_error is not None:
            self.last_error = self.global_set_error
            return False
        if not self.ignore_global_update:
            size = int.from_bytes(bytes(buffer)[2:4], "little")
            self.acls[object_type] = ctypes.string_at(buffer, size)
        return True


@pytest.mark.parametrize(("object_type", "mask"), [("File", 0x001F01FF), ("Key", 0x000F003F)])
def test_missing_global_sacl_is_created_verified_and_uses_local_free(make_policy, object_type, mask) -> None:
    api = FakeGlobalAuditApi(None)
    make_policy(api)._ensure_global_sacl(object_type, ctypes.create_string_buffer(_SID), mask)

    assert api.global_queries == [object_type, object_type]
    assert api.global_sets == [object_type]
    assert len(api.local_freed) == 1
    assert not api.freed  # Global SACLs must not be passed to AuditFree.
    assert WindowsAuditPolicy._global_sacl_aces(api.acls[object_type]) == (audit_ace(_SID, mask),)


def test_global_sacl_preserves_all_existing_entries(make_policy) -> None:
    old_entries = [audit_ace(_OTHER_SID, 0x01), audit_ace(_SID, 0x01, flags=0x40)]
    api = FakeGlobalAuditApi(global_acl(old_entries))
    make_policy(api)._ensure_global_sacl("File", ctypes.create_string_buffer(_SID), 0x03)

    assert WindowsAuditPolicy._global_sacl_aces(api.acls["File"]) == (*old_entries, audit_ace(_SID, 0x03))
    assert len(api.local_freed) == 2
    assert not api.freed


def test_existing_required_global_ace_is_not_duplicated(make_policy) -> None:
    api = FakeGlobalAuditApi(global_acl([audit_ace(_SID, 0x07)]))
    make_policy(api)._ensure_global_sacl("File", ctypes.create_string_buffer(_SID), 0x03)

    assert api.global_queries == ["File"]
    assert not api.global_sets
    assert len(api.local_freed) == 1


@pytest.mark.parametrize("error", [5, 87, 1314])
def test_non_missing_global_query_errors_remain_fatal(make_policy, error) -> None:
    api = FakeGlobalAuditApi(None)
    api.global_query_errors[1] = error
    with pytest.raises(SandboxAuditUnavailable, match=rf"code {error}\b"):
        make_policy(api)._ensure_global_sacl("File", ctypes.create_string_buffer(_SID), 0x03)

    assert not api.global_sets
    assert not api.local_freed


def test_global_sacl_write_failure_remains_fatal(make_policy) -> None:
    api = FakeGlobalAuditApi(None)
    api.global_set_error = 5
    with pytest.raises(SandboxAuditUnavailable, match="code 5"):
        make_policy(api)._ensure_global_sacl("File", ctypes.create_string_buffer(_SID), 0x03)

    assert api.global_queries == ["File"]


@pytest.mark.parametrize("error", [2, 5])
def test_global_verification_errors_are_not_treated_as_initialization(make_policy, error) -> None:
    api = FakeGlobalAuditApi(None)
    api.global_query_errors[2] = error
    with pytest.raises(SandboxAuditUnavailable, match=rf"code {error}\b"):
        make_policy(api)._ensure_global_sacl("File", ctypes.create_string_buffer(_SID), 0x03)

    assert api.global_sets == ["File"]


@pytest.mark.parametrize("verify_null", [False, True])
def test_global_sacl_without_required_rule_after_write_is_rejected(make_policy, verify_null) -> None:
    api = FakeGlobalAuditApi(global_acl([]))
    if verify_null:
        api.global_null_query = 2
    else:
        api.ignore_global_update = True
    with pytest.raises(SandboxAuditUnavailable, match="did not take effect"):
        make_policy(api)._ensure_global_sacl("File", ctypes.create_string_buffer(_SID), 0x03)

    assert len(api.local_freed) == (1 if verify_null else 2)


def test_global_sacl_successful_empty_query_is_initialized(make_policy) -> None:
    api = FakeGlobalAuditApi(global_acl([]))
    api.global_null_query = 1
    make_policy(api)._ensure_global_sacl("File", ctypes.create_string_buffer(_SID), 0x03)

    assert api.global_sets == ["File"]
    assert len(api.local_freed) == 1


def test_malformed_global_sacl_is_rejected_and_buffer_freed(make_policy) -> None:
    malformed = bytearray(global_acl([]))
    malformed[4:6] = (1).to_bytes(2, "little")
    api = FakeGlobalAuditApi(bytes(malformed))
    with pytest.raises(SandboxAuditUnavailable, match="invalid global SACL entry"):
        make_policy(api)._ensure_global_sacl("File", ctypes.create_string_buffer(_SID), 0x03)

    assert not api.global_sets
    assert len(api.local_freed) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows SID conversion and in-memory ACL APIs")
def test_first_launch_initializes_user_and_both_global_policies_without_system_writes(make_policy, monkeypatch) -> None:
    api = FakeGlobalAuditApi(None)
    policy = make_policy(api)
    monkeypatch.setattr(audit_policy, "security_privilege", nullcontext)
    monkeypatch.setattr(policy, "_ensure_event_categories", lambda: None)
    monkeypatch.setattr(policy, "_ensure_system_policy", lambda: None)

    policy.ensure("S-1-5-32-545")

    assert api.flags == [0x04, 0x04]
    assert api.global_sets == ["File", "Key"]
    assert api.global_queries == ["File", "File", "Key", "Key"]
    assert len(api.freed) == 1
    assert len(api.local_freed) == 2


@pytest.mark.skipif(os.name != "nt", reason="Native in-memory ACL construction; no system audit changes")
@pytest.mark.parametrize("with_existing", [False, True])
def test_native_acl_construction_preserves_entries_and_sid(make_policy, with_existing) -> None:
    old_entries = [audit_ace(_OTHER_SID, 0x01)] if with_existing else []
    api = FakeGlobalAuditApi(global_acl(old_entries) if with_existing else None)
    # Only native buffer construction is real. Query, persistence and free stay in-memory doubles.
    native = WindowsAuditPolicy().api
    api.InitializeAcl = native.InitializeAcl
    api.AddAce = native.AddAce
    api.AddAuditAccessAceEx = native.AddAuditAccessAceEx

    make_policy(api)._ensure_global_sacl("File", ctypes.create_string_buffer(_SID), 0x001F01FF)

    assert WindowsAuditPolicy._global_sacl_aces(api.acls["File"]) == (*old_entries, audit_ace(_SID, 0x001F01FF))
    assert not api.freed
