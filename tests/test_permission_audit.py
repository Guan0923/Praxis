from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from backend.sandbox.native_windows.audit_policy import SandboxAuditUnavailable
from backend.sandbox.native_windows.permission_audit import CommandAudit, permission_denial

SID = "S-1-5-21-1-2-3-1001"
LOGON = 0x100000002


def security_event(*, pid=123, logon=LOGON, sid=SID, record=11, failure=True, event_id=4656):
    event = ET.Element("Event", xmlns="http://schemas.microsoft.com/win/2004/08/events/event")
    system = ET.SubElement(event, "System")
    ET.SubElement(system, "Provider", Guid="{54849625-5478-4994-a5ba-3e3b0328c30d}")
    for name, value in {
        "EventID": event_id,
        "EventRecordID": record,
        "Channel": "Security",
        "Keywords": "0x8010000000000000" if failure else "0x8020000000000000",
    }.items():
        ET.SubElement(system, name).text = str(value)
    data = ET.SubElement(event, "EventData")
    fields = {
        "SubjectUserSid": sid,
        "SubjectLogonId": hex(logon),
        "ProcessId": hex(pid),
        "ObjectType": "File",
        "ObjectName": "C:/private/proof.txt",
        "AccessMask": "0x120089",
    }
    for name, value in fields.items():
        ET.SubElement(data, "Data", Name=name).text = value
    # Rendered text is irrelevant, including text deliberately contradicting the event fields.
    ET.SubElement(event, "RenderingInfo").text = "Access denied. Permission denied. Success."
    return ET.tostring(event, encoding="unicode")


@pytest.mark.parametrize(
    "change", [dict(logon=LOGON + 1), dict(sid=SID + "0"), dict(record=10), dict(failure=False), dict(event_id=4663)]
)
def test_rejects_other_command_old_success_and_wrong_event_records(change):
    assert permission_denial(security_event(**change), account_sid=SID, logon_id=LOGON, after_record=10) is None


def test_accepts_root_and_child_processes_only_in_the_reserved_logon():
    results = [
        permission_denial(security_event(pid=pid), account_sid=SID, logon_id=LOGON, after_record=10)
        for pid in (123, 456)
    ]
    assert [entry["process_id"] for entry in results] == [123, 456]


def test_audit_source_failure_is_not_treated_as_no_permission_denial():
    class Unavailable:
        def latest_record(self):
            raise SandboxAuditUnavailable()

    with pytest.raises(SandboxAuditUnavailable):
        CommandAudit(Unavailable(), SID, LOGON, 10).collect(timeout=0)


def test_reused_pid_from_another_command_is_not_evidence():
    class Source:
        def latest_record(self):
            return 13

        def lifecycle(self, _after_record, _logon_id):
            event = ET.fromstring(security_event(event_id=4634, failure=False))
            data = event.find("{*}EventData")
            ET.SubElement(
                data, "{http://schemas.microsoft.com/win/2004/08/events/event}Data", Name="TargetUserSid"
            ).text = SID
            ET.SubElement(
                data, "{http://schemas.microsoft.com/win/2004/08/events/event}Data", Name="TargetLogonId"
            ).text = hex(LOGON)
            return [ET.tostring(event, encoding="unicode")]

        def denials(self, _after_record, _logon_id):
            return [security_event(pid=123, logon=LOGON + 1), security_event(pid=456, record=12)]

    records = CommandAudit(Source(), SID, LOGON, 10, root_pid=456).collect(timeout=0)
    assert len(records) == 1
    assert records[0]["process_id"] == 456


@pytest.mark.parametrize("overwritten", [False, True])
def test_missing_audit_completion_or_checkpoint_fails_closed(overwritten):
    class Source:
        def latest_record(self):
            return 20

        def record_time(self, _record):
            return None

        def lifecycle(self, _after, _logon):
            return []

        def denials(self, _after, _logon):
            return []

    scope = CommandAudit(Source(), SID, LOGON, 10, root_pid=123, start_time="original" if overwritten else None)
    with pytest.raises(SandboxAuditUnavailable):
        scope.collect(timeout=0)
