"""Structured WFP denials, attributed by process lifetime and owned filter ID."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .audit_policy import SandboxAuditUnavailable

_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
_PROVIDER = "{54849625-5478-4994-a5ba-3e3b0328c30d}"


def network_denial(
    xml: str, *, process_id: int, after_record: int, before_record: int, filter_ids: frozenset[int]
) -> dict | None:
    try:
        event = ET.fromstring(xml)
        system = event.find("e:System", _NS)
        if system is None:
            raise ValueError("missing System")
        provider = system.find("e:Provider", _NS)
        if provider is None or provider.get("Guid", "").lower() != _PROVIDER:
            return None
        event_id = int(system.findtext("e:EventID", namespaces=_NS) or "0")
        record = int(system.findtext("e:EventRecordID", namespaces=_NS) or "0")
        if (
            system.findtext("e:Channel", namespaces=_NS) != "Security"
            or event_id not in {5155, 5157, 5159}
            or not after_record < record < before_record
            or not int(system.findtext("e:Keywords", namespaces=_NS) or "0", 0) & 0x0010000000000000
        ):
            return None
        fields = {entry.attrib["Name"]: entry.text or "" for entry in event.findall("e:EventData/e:Data", _NS)}
        pid = int(fields["ProcessID"], 0)
        filter_id = int(fields["FilterRTID"], 0)
        if pid != process_id or filter_id not in filter_ids:
            return None
        return {
            "source": "windows_wfp",
            "event_id": event_id,
            "record_id": record,
            "process_id": pid,
            "filter_id": filter_id,
            "source_address": fields.get("SourceAddress"),
            "source_port": fields.get("SourcePort"),
            "destination_address": fields.get("DestAddress"),
            "destination_port": fields.get("DestPort"),
            "protocol": fields.get("Protocol"),
        }
    except (ET.ParseError, KeyError, TypeError, ValueError) as exc:
        raise SandboxAuditUnavailable("Windows returned an invalid network audit event.") from exc
