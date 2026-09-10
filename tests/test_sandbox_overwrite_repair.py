from __future__ import annotations

import json
import os
import subprocess
import sys
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest
from fastapi import Request

from backend.api.routes.sandbox import install, repair, router
from backend.sandbox import SandboxMaintenanceGate
from backend.sandbox import install_helper as helper
from backend.sandbox.broker_service import BrokerCredentialPackage, build_ready_marker
from backend.sandbox.broker_service.installer import _write_python_service_path
from backend.sandbox.installation import access_policy
from backend.sandbox.installation.contracts import EXIT_SERVICE_START_FAILED, TransactionFailure
from backend.sandbox.installation.lock import installation_lock


@pytest.fixture
def transaction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    data = tmp_path / "Praxis" / "SandboxBroker"
    data.mkdir(parents=True)
    source = tmp_path / "repo" / "backend" / "src"
    source.mkdir(parents=True)
    calls: list[str] = []
    package = BrokerCredentialPackage(
        "generation-test",
        "PraxisSbxOffline",
        "S-1-5-21-1-2-3-1001",
        "offline-test",
        "PraxisSbxOnline",
        "S-1-5-21-1-2-3-1002",
        "online-test",
    )
    payload = {
        "operation": "repair",
        "service_name": "PraxisSandboxBroker",
        "service_command": [str(tmp_path / "runtime" / "pythonservice.exe")],
        "service_class": rf"{source}\sandbox_service_bootstrap.PraxisSandboxBrokerService",
        "backend_sid": "S-1-5-21-1-2-3-500",
        "backend_sid_path": str(data / "backend.sid"),
        "program_data_path": str(data),
        "service_code_path": str(source),
        "service_code_boundary_path": str(tmp_path / "repo"),
    }

    @contextmanager
    def lock(_name):
        calls.append("lock")
        try:
            yield
        finally:
            calls.append("unlock")

    def host(command, _class):
        calls.append("host")
        return command

    def run(command, **_kwargs):
        calls.append(" ".join(command[:2]))

    monkeypatch.setattr(helper, "installation_lock", lock)
    monkeypatch.setattr(helper, "_service_exists", lambda _name: True)
    monkeypatch.setattr(helper, "_stop_service_for_repair", lambda _name: calls.append("stop"))
    monkeypatch.setattr(helper, "_prepare_service_host", host)
    monkeypatch.setattr(helper, "_initialize_installation_key", lambda _path: calls.append("key"))
    monkeypatch.setattr(
        helper, "_provision_fixed_accounts", lambda *_args: (package, build_ready_marker(package, 17831))
    )
    monkeypatch.setattr(helper, "_secure_program_data", lambda *_args: calls.append("data-acl"))
    monkeypatch.setattr(helper, "_secure_source_code", lambda *_args: calls.append("source-acl"))
    monkeypatch.setattr(helper, "_run", run)
    return types.SimpleNamespace(data=data, payload=payload, calls=calls)


@pytest.mark.parametrize("installed", [False, True])
def test_create_or_overwrite_without_removing_existing_files(transaction, monkeypatch, installed):
    monkeypatch.setattr(helper, "_service_exists", lambda _name: installed)
    kept = {
        "ready.json": "old-ready",
        "legacy-artifact.txt": "leave-here",
        "installation.key.dpapi": "existing-key",
        "installation.id": "existing-id",
    }
    for name, content in kept.items():
        (transaction.data / name).write_text(content, encoding="utf-8")
    for _ in range(2):
        assert helper.run_transaction(transaction.payload) == 0
        marker = json.loads((transaction.data / "ready.json").read_text())
        assert marker["generation"] == "generation-test"
        for name, content in kept.items():
            if name != "ready.json":
                assert (transaction.data / name).read_text() == content
    assert ("sc.exe config" if installed else "sc.exe create") in transaction.calls
    assert "sc.exe delete" not in transaction.calls
    assert transaction.calls[0] == "lock" and transaction.calls[-1] == "unlock"
    assert transaction.calls.index("host") < transaction.calls.index("key")
    if installed:
        assert transaction.calls.index("stop") < transaction.calls.index("host")
    else:
        assert "stop" not in transaction.calls


@pytest.mark.parametrize("phase", ["_prepare_service_host", "_secure_source_code"])
def test_failed_preparation_keeps_previous_ready_file(transaction, monkeypatch, phase):
    ready = transaction.data / "ready.json"
    ready.write_text("previous-marker")

    def fail(*_args):
        raise OSError("test preparation failure")

    monkeypatch.setattr(helper, phase, fail)
    with pytest.raises(OSError, match="test preparation failure"):
        helper.run_transaction(transaction.payload)
    assert ready.read_text() == "previous-marker"
    assert transaction.calls[-1] == "unlock"


def test_failed_service_start_keeps_new_marker(transaction, monkeypatch):
    def run(command, **_kwargs):
        if command[:2] == ["sc.exe", "start"]:
            raise TransactionFailure(EXIT_SERVICE_START_FAILED, "start failed")

    monkeypatch.setattr(helper, "_run", run)
    with pytest.raises(TransactionFailure, match="start failed"):
        helper.run_transaction(transaction.payload)
    assert json.loads((transaction.data / "ready.json").read_text())["generation"] == "generation-test"
    assert transaction.calls[-1] == "unlock"


def test_host_configuration_overwrites_current_file_and_leaves_old_files(tmp_path):
    host = tmp_path / "pythonservice.exe"
    old = host.with_suffix("._pth")
    old.write_text("leave old version alone")
    current = tmp_path / f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    current.write_text("obsolete current configuration")
    target = _write_python_service_path(host, tmp_path, tmp_path)
    assert target == current
    assert "obsolete" not in target.read_text()
    assert old.read_text() == "leave old version alone"


@pytest.mark.parametrize("entrypoint", [install, repair])
def test_concurrent_routes_share_maintenance_guard(tmp_path, entrypoint):
    entered, release = Event(), Event()
    calls = []

    class Broker:
        def status(self):
            return {"installed": True, "healthy": True}

        def repair(self):
            calls.append("repair")
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release not signaled")
            return {"installed": True, "healthy": True}

    state = types.SimpleNamespace(
        sandbox_broker=Broker(),
        sandbox_maintenance=SandboxMaintenanceGate(),
        sandbox_manifest_path=tmp_path / "resources.json",
    )
    request = Request({"type": "http", "app": types.SimpleNamespace(state=types.SimpleNamespace(web=state))})
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(entrypoint, request)
        try:
            assert entered.wait(5)
            second = repair(request)
            assert second.status_code == 409
            assert json.loads(second.body)["code"] == "broker_maintenance_busy"
        finally:
            release.set()
        assert first.result()["healthy"] is True
    assert calls == ["repair"]
    assert not state.sandbox_maintenance.maintenance_active


def test_existing_account_credentials_are_preserved(monkeypatch, tmp_path):
    from backend.sandbox.broker_service.credentials import DpapiCredentialStore
    from backend.sandbox.installation import accounts

    package = BrokerCredentialPackage(
        "existing-generation",
        accounts.OFFLINE_ACCOUNT,
        "S-1-5-21-1-2-3-1001",
        "offline-test",
        accounts.ONLINE_ACCOUNT,
        "S-1-5-21-1-2-3-1002",
        "online-test",
    )
    users = {package.offline_name: package.offline_sid, package.online_name: package.online_sid}
    writes, saved, network = [], [], []

    class ExistingGroupError(Exception):
        winerror = 1379

    def existing_group(*_args):
        raise ExistingGroupError()

    net = types.SimpleNamespace(
        NetLocalGroupAdd=existing_group,
        NetLocalGroupGetInfo=lambda *_args: {"comment": accounts.GROUP_COMMENT},
        NetUserGetInfo=lambda *_args: {"priv": 0, "flags": 0, "comment": accounts.ACCOUNT_COMMENT},
        NetUserGetLocalGroups=lambda *_args: [accounts.ACCOUNT_GROUP],
        NetUserSetInfo=lambda _server, name, level, _info: writes.append((name, level)),
        NetLocalGroupAddMembers=lambda *_args: None,
    )
    rights = {}
    security = types.SimpleNamespace(
        POLICY_ALL_ACCESS=1,
        LookupAccountName=lambda _host, name: (users.get(name, "group-sid"), "HOST", 1),
        ConvertSidToStringSid=lambda sid: sid,
        ConvertStringSidToSid=lambda sid: sid,
        LsaOpenPolicy=lambda *_args: object(),
        LsaAddAccountRights=lambda _handle, sid, values: rights.update({sid: values}),
        LsaEnumerateAccountRights=lambda _handle, sid: rights[sid],
    )
    monkeypatch.setitem(sys.modules, "win32net", net)
    monkeypatch.setitem(sys.modules, "win32security", security)
    monkeypatch.setattr(accounts, "credential_works", lambda *_args: True)
    monkeypatch.setattr(DpapiCredentialStore, "load", lambda _self: package)
    monkeypatch.setattr(DpapiCredentialStore, "save", lambda _self, value: saved.append(value))
    result, marker = accounts.provision_fixed_accounts(
        tmp_path,
        "PraxisSandboxBroker",
        17831,
        configure_network=lambda *args: network.append(args),
    )
    assert result == package and saved == [package]
    assert marker["generation"] == package.generation
    assert writes == [(package.offline_name, 1008), (package.online_name, 1008)]
    assert network == [(package.offline_sid, package.online_sid, 17831)]


def test_service_query_error_does_not_mean_missing(monkeypatch):
    monkeypatch.setattr(helper.subprocess, "run", lambda *_args, **_kwargs: types.SimpleNamespace(returncode=5))
    with pytest.raises(TransactionFailure, match="service state is unavailable"):
        helper._service_exists("PraxisSandboxBroker")


def test_existing_marker_does_not_hide_service_failure(transaction):
    from backend.sandbox import WindowsBrokerClient
    from backend.sandbox.broker_service.readiness import write_ready_marker

    helper.run_transaction(transaction.payload)
    ready = transaction.data / "ready.json"
    marker = json.loads(ready.read_text())
    write_ready_marker(ready, marker)

    def unavailable(_payload):
        raise OSError(22, "Invalid argument")

    installer = types.SimpleNamespace(service_installed=lambda: True, configuration_healthy=lambda: True)
    client = WindowsBrokerClient(
        installer=installer,
        installation_key=b"t" * 32,
        ready_path=ready,
        transport=unavailable,
        is_windows=True,
    )
    status = client.status()
    assert status.installed and not status.healthy
    assert status.code.value == "broker_pipe_unavailable"


def test_reinstall_route_is_removed():
    assert "/api/sandbox/reinstall" not in {route.path for route in router.routes}


@pytest.mark.skipif(os.name != "nt", reason="Real Windows ACL test")
def test_real_acl_updates_only_declared_objects_and_is_idempotent(tmp_path):
    import win32security

    from backend.sandbox.native_windows.security import _set_directory_dacl_direct

    root = tmp_path / "repo"
    source = root / "backend" / "src"
    source.mkdir(parents=True)
    unrelated = root / "unrelated"
    unrelated.mkdir()
    sentinel = unrelated / "sentinel.txt"
    sentinel.write_text("untouched")
    module = source / "module.py"
    module.write_text("pass")
    service_name = f"PraxisAclTest-{uuid.uuid4().hex}"
    other_sid = win32security.ConvertStringSidToSid("S-1-5-21-1-2-3-9876")

    def descriptor(path):
        return win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )

    def sddl(path):
        return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
            descriptor(path), 1, win32security.DACL_SECURITY_INFORMATION
        )

    # Stage an inheritable ACE only on the parent. A propagating write would
    # copy this unrelated ACE to descendants and fail the sentinel check.
    dacl = descriptor(root).GetSecurityDescriptorDacl()
    dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, 3, 0x20, other_sid)
    _set_directory_dacl_direct(root, dacl)
    untouched = {path: sddl(path) for path in (unrelated, sentinel)}
    access_policy._secure_source_code(source, root, service_name)
    assert {path: sddl(path) for path in untouched} == untouched
    targets = (root, root / "backend", source, module)
    after = {path: sddl(path) for path in targets}
    access_policy._secure_source_code(source, root, service_name)
    for path in targets:
        assert sddl(path) == after[path], str(path)
    service_sid = win32security.ConvertStringSidToSid(access_policy._service_sid(service_name))
    assert any(
        descriptor(module).GetSecurityDescriptorDacl().GetAce(i)[2] == service_sid
        for i in range(descriptor(module).GetSecurityDescriptorDacl().GetAceCount())
    )
    future = source / "future.py"
    future.write_text("pass")
    dacl = descriptor(future).GetSecurityDescriptorDacl()
    assert any(dacl.GetAce(i)[2] == service_sid for i in range(dacl.GetAceCount()))


@pytest.mark.skipif(os.name != "nt", reason="Real Windows directory link test")
def test_real_source_acl_does_not_follow_directory_links(tmp_path):
    import win32security

    source = tmp_path / "repo" / "backend" / "src"
    source.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched")
    try:
        os.symlink(outside, source / "linked", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlink creation unavailable: {exc.winerror}")

    def sddl(path):
        value = win32security.GetNamedSecurityInfo(str(path), 1, win32security.DACL_SECURITY_INFORMATION)
        return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
            value, 1, win32security.DACL_SECURITY_INFORMATION
        )

    before = [sddl(outside), sddl(sentinel)]
    access_policy._secure_source_code(source, tmp_path / "repo", f"PraxisLinkTest-{uuid.uuid4().hex}")
    assert [sddl(outside), sddl(sentinel)] == before


@pytest.mark.skipif(os.name != "nt", reason="Real Windows process lock test")
def test_real_installation_lock_blocks_another_process_and_releases():
    name = f"PraxisLockTest-{uuid.uuid4().hex}"
    script = """
import sys
from backend.sandbox.installation.lock import installation_lock
from backend.sandbox.installation.contracts import TransactionFailure
try:
    with installation_lock(sys.argv[1]):
        print('acquired')
except TransactionFailure as exc:
    print(exc.exit_code)
"""

    def probe():
        return subprocess.run(
            [sys.executable, "-c", script, name], check=True, capture_output=True, text=True, timeout=15
        ).stdout.strip()

    with installation_lock(name):
        assert probe() == "12"
    assert probe() == "acquired"
