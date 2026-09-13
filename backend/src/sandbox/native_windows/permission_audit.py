"""Permission denial evidence from structured Windows Security events only."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .audit_policy import SandboxAuditUnavailable, WindowsAuditPolicy
from .network_audit import network_denial

_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
_PROVIDER = "{54849625-5478-4994-a5ba-3e3b0328c30d}"
_AUDIT_FAILURE = 0x0010000000000000


def permission_denial(xml: str, *, account_sid: str, logon_id: int, after_record: int) -> dict | None:
    """Validate the provider, failure keyword, account and per-command logon identity."""

    try:
        event = ET.fromstring(xml)
        system = event.find("e:System", _NS)
        if system is None:
            raise ValueError("missing System")
        provider = system.find("e:Provider", _NS)
        if provider is None or provider.get("Guid", "").lower() != _PROVIDER:
            return None
        if system.findtext("e:Channel", namespaces=_NS) != "Security":
            return None
        event_id = int(system.findtext("e:EventID", namespaces=_NS) or "0")
        if event_id != 4656:
            return None
        record_id = int(system.findtext("e:EventRecordID", namespaces=_NS) or "0")
        keywords = int(system.findtext("e:Keywords", namespaces=_NS) or "0", 0)
        if record_id <= after_record or not keywords & _AUDIT_FAILURE:
            return None
        fields = {entry.attrib["Name"]: entry.text or "" for entry in event.findall("e:EventData/e:Data", _NS)}
        if fields["SubjectUserSid"] != account_sid or int(fields["SubjectLogonId"], 0) != logon_id:
            return None
        process_id = int(fields["ProcessId"], 0)
        if process_id <= 0 or fields["ObjectType"] not in {"File", "Key"}:
            return None
        return {
            "event_id": event_id,
            "record_id": record_id,
            "process_id": process_id,
            "logon_id": logon_id,
            "object_type": fields["ObjectType"],
            "object_name": fields["ObjectName"],
            "access_mask": int(fields["AccessMask"], 0),
        }
    except (ET.ParseError, KeyError, TypeError, ValueError) as exc:
        raise SandboxAuditUnavailable("Windows returned an invalid permission audit event.") from exc


class SecurityEventSource:
    def __init__(self) -> None:
        import win32evtlog

        self.api = win32evtlog

    def _events(self, query: str, *, reverse: bool = False, limit: int = 256) -> list[str]:
        api = self.api
        flags = api.EvtQueryChannelPath | (api.EvtQueryReverseDirection if reverse else api.EvtQueryForwardDirection)
        handle = None
        try:
            handle = api.EvtQuery("Security", flags, query)
            result = []
            while len(result) < limit:
                batch = api.EvtNext(handle, min(32, limit - len(result)), 0, 0)
                if not batch:
                    break
                for event in batch:
                    try:
                        result.append(api.EvtRender(event, api.EvtRenderEventXml))
                    finally:
                        event.Close()
            if len(result) == limit and not reverse:
                raise SandboxAuditUnavailable("Command permission audit event limit exceeded.")
            return result
        except SandboxAuditUnavailable:
            raise
        except Exception as exc:
            raise SandboxAuditUnavailable("Cannot read the Windows Security event log.") from exc
        finally:
            if handle is not None:
                handle.Close()

    def latest_record(self) -> int:
        values = self._events("*", reverse=True, limit=1)
        if not values:
            return 0
        return int(ET.fromstring(values[0]).findtext("e:System/e:EventRecordID", namespaces=_NS) or "0")

    def record_time(self, record_id: int) -> str | None:
        values = self._events(f"*[System[EventRecordID={record_id}]]", reverse=True, limit=1)
        if not values:
            return None
        stamp = ET.fromstring(values[0]).find("e:System/e:TimeCreated", _NS)
        return stamp.get("SystemTime") if stamp is not None else None

    def denials(self, after_record: int, logon_id: int) -> list[str]:
        # Numeric identities come from the verified process token, never command output.
        query = (
            f"*[System[(EventID=4656) and (EventRecordID > {after_record})]]"
            f" and *[EventData[Data[@Name='SubjectLogonId']='{hex(logon_id)}']]"
        )
        return self._events(query)

    def lifecycle(self, after_record: int, logon_id: int) -> list[str]:
        query = (
            f"*[System[((EventID=4688) or (EventID=4689) or (EventID=4634)) and (EventRecordID > {after_record})]]"
            f" and *[EventData[(Data[@Name='SubjectLogonId']='{hex(logon_id)}')"
            f" or (Data[@Name='TargetLogonId']='{hex(logon_id)}')]]"
        )
        return self._events(query)

    def network_denials(self, after_record: int, before_record: int, process_id: int) -> list[str]:
        query = (
            f"*[System[((EventID=5155) or (EventID=5157) or (EventID=5159))"
            f" and (EventRecordID > {after_record}) and (EventRecordID < {before_record})]]"
            f" and *[EventData[(Data[@Name='ProcessID']='{process_id}') or (Data[@Name='ProcessID']='{hex(process_id)}')]]"
        )
        return self._events(query)


@dataclass
class CommandAudit:
    source: SecurityEventSource
    account_sid: str
    logon_id: int
    start_record: int
    root_pid: int = 0
    start_time: str | None = None
    block_filter_ids: frozenset[int] = frozenset()
    proxy_denials: tuple[dict, ...] = ()

    def bind_process(self, process_id: int, logon_sid: str) -> None:
        import win32api
        import win32security

        process = token = None
        try:
            process = win32api.OpenProcess(0x1000, False, process_id)
            token = win32security.OpenProcessToken(process, win32security.TOKEN_QUERY)
            user, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
            if win32security.ConvertSidToStringSid(user) != self.account_sid or not any(
                win32security.ConvertSidToStringSid(sid) == logon_sid for sid, _ in groups
            ):
                raise SandboxAuditUnavailable("Command token does not match its Broker reservation.")
            # LUA_TOKEN can create a new AuthenticationId while retaining the original logon SID.
            self.logon_id = int(
                win32security.GetTokenInformation(token, win32security.TokenStatistics)["AuthenticationId"]
            )
            self.root_pid = process_id
        except Exception as exc:
            raise SandboxAuditUnavailable("Cannot verify the command process audit identity.") from exc
        finally:
            if token is not None:
                token.Close()
            if process is not None:
                process.Close()

    def collect(self, *, timeout: float = 10.0) -> tuple[dict, ...]:
        deadline = time.monotonic() + timeout
        while True:
            if self.source.latest_record() < self.start_record:
                raise SandboxAuditUnavailable("The Windows Security log was cleared during command execution.")
            if self.start_time is not None and self.source.record_time(self.start_record) != self.start_time:
                raise SandboxAuditUnavailable("The command audit checkpoint was cleared or overwritten.")
            processes = {self.root_pid}
            lifetimes = {self.root_pid: [self.start_record, None]}
            previous_lifetimes = []
            closed = 0
            for xml in self.source.lifecycle(self.start_record, self.logon_id):
                event = ET.fromstring(xml)
                system = event.find("e:System", _NS)
                provider = system.find("e:Provider", _NS)
                if provider is None or provider.get("Guid", "").lower() != _PROVIDER:
                    continue
                if (
                    system.findtext("e:Channel", namespaces=_NS) != "Security"
                    or int(system.findtext("e:EventRecordID", namespaces=_NS) or "0") <= self.start_record
                    or not int(system.findtext("e:Keywords", namespaces=_NS) or "0", 0) & 0x0020000000000000
                ):
                    continue
                fields = {entry.attrib["Name"]: entry.text or "" for entry in event.findall("e:EventData/e:Data", _NS)}
                event_id = int(system.findtext("e:EventID", namespaces=_NS) or "0")
                record_id = int(system.findtext("e:EventRecordID", namespaces=_NS) or "0")
                if (
                    event_id == 4634
                    and fields.get("TargetUserSid") == self.account_sid
                    and int(fields["TargetLogonId"], 0) == self.logon_id
                ):
                    closed = record_id
                elif event_id == 4688:
                    child = int(fields["NewProcessId"], 0)
                    if child == self.root_pid or int(fields["ProcessId"], 0) in processes:
                        processes.add(child)
                        if child in lifetimes and lifetimes[child][1] is not None:
                            previous_lifetimes.append((child, lifetimes[child]))
                        lifetimes[child] = [record_id, None]
                elif event_id == 4689 and int(fields["ProcessId"], 0) in processes:
                    lifetimes[int(fields["ProcessId"], 0)][1] = record_id
            denials = tuple(
                evidence
                for xml in self.source.denials(self.start_record, self.logon_id)
                if (
                    evidence := permission_denial(
                        xml, account_sid=self.account_sid, logon_id=self.logon_id, after_record=self.start_record
                    )
                )
                is not None
                and evidence["process_id"] in processes
            )
            if closed:
                network = []
                if self.block_filter_ids:
                    for pid, (born, ended) in [*previous_lifetimes, *lifetimes.items()]:
                        end = ended or closed
                        for xml in self.source.network_denials(born, end, pid):
                            evidence = network_denial(
                                xml,
                                process_id=pid,
                                after_record=born,
                                before_record=end,
                                filter_ids=self.block_filter_ids,
                            )
                            if evidence is not None:
                                evidence["logon_id"] = self.logon_id
                                network.append(evidence)
                return denials + tuple(network) + self.proxy_denials
            if time.monotonic() >= deadline:
                raise SandboxAuditUnavailable("Windows did not confirm the command audit session had closed.")
            time.sleep(0.05)


class WindowsSecurityAudit:
    def __init__(self) -> None:
        self._accounts: set[str] = set()

    def prepare(self) -> None:
        from ..broker_service import BrokerConfiguration, read_ready_marker

        marker = read_ready_marker(BrokerConfiguration.create().ready_path)
        policy = WindowsAuditPolicy()
        for role in ("offline", "online"):
            account_sid = marker["accounts"][role]["sid"]
            policy.ensure(account_sid)
            self._accounts.add(account_sid)
        SecurityEventSource().latest_record()

    def start(self, account_sid: str) -> CommandAudit:
        from .wfp import sandbox_block_filter_ids

        if account_sid not in self._accounts:
            raise SandboxAuditUnavailable("Broker account does not match the configured permission audit accounts.")
        source = SecurityEventSource()
        record = source.latest_record()
        stamp = source.record_time(record)
        if stamp is None:
            raise SandboxAuditUnavailable("Cannot establish a Windows Security audit checkpoint.")
        filters = sandbox_block_filter_ids()
        if not filters:
            raise SandboxAuditUnavailable("Cannot identify the installed sandbox network block filters.")
        return CommandAudit(source, account_sid, 0, record, start_time=stamp, block_filter_ids=filters)
