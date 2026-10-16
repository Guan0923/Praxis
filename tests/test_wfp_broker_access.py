from __future__ import annotations

import ctypes
import os
from types import SimpleNamespace

import pytest

from backend.sandbox.native_windows import wfp

SERVICE = "S-1-5-80-1-2-3-4-5"
OFFLINE = "S-1-5-21-1-2-3-1001"
ONLINE = "S-1-5-21-1-2-3-1002"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows ACL encoding")
def test_filter_grant_preserves_existing_aces_is_minimal_and_idempotent(monkeypatch):
    import win32security

    acl = win32security.ACL()
    admin = win32security.ConvertStringSidToSid("S-1-5-32-544")
    other = win32security.ConvertStringSidToSid("S-1-5-21-10-20-30-40")
    acl.AddAccessDeniedAce(win32security.ACL_REVISION, 0x400, other)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, 0xF07FF, admin)
    previous = [acl.GetAce(i) for i in range(acl.GetAceCount())]
    monkeypatch.setattr(wfp, "_read_filter_dacl", lambda *_args: acl)
    writes = []

    def set_security(*args):
        pointer = ctypes.cast(args[5], ctypes.c_void_p).value
        length = ctypes.c_ushort.from_address(pointer + 2).value
        writes.append((args, ctypes.string_at(pointer, length)))
        return 0

    api = SimpleNamespace(
        library=SimpleNamespace(FwpmFilterSetSecurityInfoByKey0=set_security),
        check=wfp._WfpApi.check,
    )
    wfp._grant_filter_access(api, "engine", None, SERVICE, wfp._FWPM_ACTRL_ENUM)
    wfp._grant_filter_access(api, "engine", None, SERVICE, wfp._FWPM_ACTRL_ENUM)
    assert len(writes) == 1
    current = [acl.GetAce(i) for i in range(acl.GetAceCount())]
    assert all(ace in current for ace in previous)
    sid = win32security.ConvertStringSidToSid(SERVICE)
    added = [ace for ace in current if ace[-1] == sid]
    assert len(added) == 1 and added[0][:2] == ((0, 0), 0x20)
    arguments, encoded_acl = writes[0]
    assert len(encoded_acl) >= 8 and encoded_acl[0] in (2, 4)
    assert int.from_bytes(encoded_acl[4:6], "little") == acl.GetAceCount()
    assert arguments[2] == 4
    assert arguments[3:5] == (None, None)
    assert arguments[-1] is None


@pytest.mark.skipif(os.name != "nt", reason="Native Windows ACL encoding")
def test_filter_read_grant_does_not_inherit_or_grant_write(monkeypatch):
    import win32security

    acl = win32security.ACL()
    monkeypatch.setattr(wfp, "_read_filter_dacl", lambda *_args: acl)
    api = SimpleNamespace(
        library=SimpleNamespace(FwpmFilterSetSecurityInfoByKey0=lambda *_args: 0), check=wfp._WfpApi.check
    )
    wfp._grant_filter_access(api, "engine", "owned-filter", SERVICE, wfp._FWPM_ACTRL_READ)
    assert acl.GetAce(0)[:2] == ((0, 0), 0x80)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows ACL encoding")
@pytest.mark.parametrize("result", [5, 0])
def test_grant_failure_or_missing_readback_fails_closed(monkeypatch, result):
    import win32security

    monkeypatch.setattr(wfp, "_read_filter_dacl", lambda *_args: win32security.ACL())
    api = SimpleNamespace(
        library=SimpleNamespace(FwpmFilterSetSecurityInfoByKey0=lambda *_args: result), check=wfp._WfpApi.check
    )
    with pytest.raises(OSError):
        wfp._grant_filter_access(api, "engine", None, SERVICE, wfp._FWPM_ACTRL_ENUM)


@pytest.mark.parametrize("fail_grant", [False, True])
def test_configuration_grants_only_container_and_owned_filters_after_commit(monkeypatch, fail_grant):
    calls = []

    def call(name):
        def invoke(*_args):
            calls.append(name)
            return 0

        return invoke

    library = SimpleNamespace(
        **{
            name: call(name)
            for name in (
                "FwpmEngineOpen0",
                "FwpmEngineClose0",
                "FwpmTransactionBegin0",
                "FwpmTransactionCommit0",
                "FwpmTransactionAbort0",
                "FwpmSubLayerDeleteByKey0",
                "FwpmProviderDeleteByKey0",
                "FwpmProviderAdd0",
                "FwpmSubLayerAdd0",
            )
        }
    )
    monkeypatch.setattr(
        wfp, "_WfpApi", lambda: SimpleNamespace(library=library, check=lambda code, *_args, **_kwargs: None)
    )
    monkeypatch.setattr(wfp, "_delete_owned_filters", lambda *_args: calls.append("delete-owned"))
    monkeypatch.setattr(wfp, "_add_filter", lambda *_args: calls.append("add-filter"))
    granted = []

    def grant(_api, _engine, key, sid, rights):
        assert "FwpmTransactionCommit0" in calls
        if fail_grant:
            raise OSError("grant failed")
        key_bytes = bytes(ctypes.cast(key, ctypes.POINTER(wfp._Guid)).contents) if key else None
        granted.append((key_bytes, sid, rights))

    monkeypatch.setattr(wfp, "_grant_filter_access", grant)
    if fail_grant:
        with pytest.raises(OSError, match="grant failed"):
            wfp.configure_static_wfp(OFFLINE, ONLINE, 17831, service_sid=SERVICE)
        assert "FwpmTransactionAbort0" not in calls
    else:
        wfp.configure_static_wfp(OFFLINE, ONLINE, 17831, service_sid=SERVICE)
        specs = wfp.build_static_filter_specs(OFFLINE, ONLINE, 17831)
        assert granted == [(None, SERVICE, 0x20)] + [
            (bytes(wfp._Guid.from_uuid(spec.key)), SERVICE, 0x80) for spec in specs
        ]
    assert calls[-1] == "FwpmEngineClose0"


@pytest.mark.parametrize("sid", [OFFLINE, "invalid", "S-1-5-80-1;bad"])
def test_non_service_reader_is_rejected_before_native_calls(monkeypatch, sid):
    monkeypatch.setattr(wfp, "_WfpApi", lambda: pytest.fail("must not touch native WFP"))
    with pytest.raises(ValueError, match="service SID"):
        wfp.configure_static_wfp(OFFLINE, ONLINE, 17831, service_sid=sid)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows security descriptor encoding")
@pytest.mark.parametrize("null_dacl", [False, True])
def test_read_filter_dacl_copies_and_frees_native_descriptor(monkeypatch, null_dacl):
    import win32security

    descriptor = win32security.SECURITY_DESCRIPTOR()
    acl = win32security.ACL()
    if not null_dacl:
        acl.AddAccessAllowedAce(2, 0x80, win32security.ConvertStringSidToSid(SERVICE))
    descriptor.SetSecurityDescriptorDacl(1, None if null_dacl else acl, 0)
    data = bytes(descriptor)
    buffer = ctypes.create_string_buffer(data)
    freed = []

    def get(_engine, _key, _info, _owner, _group, _dacl, _sacl, result):
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
        return 0

    api = SimpleNamespace(
        library=SimpleNamespace(
            FwpmFilterGetSecurityInfoByKey0=get, FwpmFreeMemory0=lambda pointer: freed.append(True)
        ),
        advapi=SimpleNamespace(GetSecurityDescriptorLength=lambda _descriptor: len(data)),
        check=wfp._WfpApi.check,
    )
    if null_dacl:
        with pytest.raises(OSError, match="null DACL"):
            wfp._read_filter_dacl(api, "engine", None)
    else:
        result = wfp._read_filter_dacl(api, "engine", None)
        assert result.GetAce(0)[:2] == ((0, 0), 0x80)
    assert freed == [True]


def test_install_helper_uses_actual_configured_service_sid(monkeypatch):
    from backend.sandbox import install_helper

    calls = []
    monkeypatch.setattr(
        install_helper, "_service_sid", lambda name: SERVICE if name == "TestBroker" else pytest.fail(name)
    )
    monkeypatch.setattr(wfp, "configure_static_wfp", lambda *args, **kwargs: calls.append((args, kwargs)))
    install_helper._configure_static_network(OFFLINE, ONLINE, 17831, service_name="TestBroker")
    assert calls == [((OFFLINE, ONLINE, 17831), {"service_sid": SERVICE})]
