# examples/ad_collector.py
"""
Active Directory Collector — Domain Controller Security Event Log,
covering account lifecycle AND file/share access, not just logon.

DEPLOYMENT PREREQUISITE, not optional: point evtx_path at a Security.evtx
exported from a DOMAIN CONTROLLER, not a random workstation. Windows
Security Event Log vs. Active Directory are related but genuinely
different things — see prior discussion. AD account/group lifecycle
events (4720/4726/4732/4738) only fire meaningfully where the AD database
actually lives (the DC); a workstation has no AD objects to change and
will essentially never emit them.

AUDIT POLICY PREREQUISITES, not optional — none of these event IDs are
guaranteed to be logged just because the collector is running. Each
requires the corresponding Windows Advanced Audit Policy subcategory
enabled on the DC (Computer Configuration -> Windows Settings -> Security
Settings -> Advanced Audit Policy Configuration -> Audit Policies):
  - Account Management            -> 4720, 4726, 4732, 4738
  - Logon/Logoff                  -> 4624, 4625, 4634
  - Object Access > File System   -> 4663 (also needs a SACL configured
                                      on the specific files/folders being
                                      monitored — enabling the audit
                                      policy alone does nothing without
                                      that per-object SACL)
  - Object Access > File Share    -> 5140
  - Object Access > Detailed File Share -> 5145 (NOT enabled by default
                                      here — see below)
These are frequently OFF by default. A DC with default audit settings
will produce logon events fine and silently zero account-lifecycle or
file-access events, with nothing in this collector able to tell you why
— that gap is a Windows configuration issue, not something this code can
detect or work around.

Scope, extending windows_eventlog_collector.py's logon/account set with
Object Access (file and share access):
  - 4663 "An attempt was made to access an object" (local/direct file
    access). Per Microsoft's own documentation this event ID is ALWAYS a
    Success event — there is no 4663 Failure variant (a denied attempt
    shows up differently, via 4656 with a Failure keyword, which is NOT
    included here: 4656 requires the separate "Handle Manipulation"
    subcategory and fires on every object OPEN regardless of whether any
    permission was ever exercised, making it a much noisier and less
    precise signal than 4663).
  - 5140 "A network share object was accessed" (share-level connection,
    coarse-grained — logs the share connection, not which files within
    it were touched). Result varies per event, read from Keywords (see
    below), unlike the fixed-outcome events elsewhere in this table.
  - 5145 (per-file access check on a network share, fine-grained) is
    documented as extremely high volume ("every access to every file via
    network shares") and is deliberately NOT in the default event ID
    table. Add it yourself if you need file-level granularity on share
    access and can handle the volume:
        EVENT_ID_ACTIONS["5145"] = ("ShareFileAccessCheck", None)

Result derivation is NOT uniform across this table, unlike
windows_eventlog_collector.py where every event ID has a fixed outcome.
4663 is always Success (baked into the table). 5140 (and 5145, if added)
can be either, signaled by the event's Keywords element rather than the
event ID itself — 0x8020000000000000 = Success, 0x8010000000000000 =
Failure (values confirmed against AWS FSx's file-access-auditing
documentation, not guessed). EVENT_ID_ACTIONS entries use None as the
result to mean "read Keywords dynamically" instead of a fixed string.

Field mapping is declarative — see ad_collector.fieldmap.json. As with
windows_eventlog_collector.py, the event-ID (and now also Keywords)
lookup is a table-driven inclusion/enrichment decision that stays in
Python (poll_records/_parse_record); the field map only does plain path
resolution against the flat dict that produces, including a fallback
chain for resource.resource_name (ShareName, then ObjectName, then
Computer) — file/share events populate ShareName or ObjectName, logon/
account events populate neither and fall through to Computer.

Deliberately a SEPARATE file from windows_eventlog_collector.py, not a
modification of it: they target different deployment scenarios (any
Windows host vs. specifically a DC) and every collector in this project
is meant to be self-contained and independently copyable — importing
shared XML-parsing code between the two would break that.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

_EVENT_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}

# event_id -> (action, fixed_result). fixed_result of None means "read
# the event's Keywords element instead" -- see module docstring.
EVENT_ID_ACTIONS: dict[str, tuple[str, Optional[str]]] = {
    "4624": ("Login", "Success"),
    "4625": ("Login", "Failure"),
    "4634": ("Logoff", "Success"),
    "4720": ("CreateUser", "Success"),
    "4726": ("DeleteUser", "Success"),
    "4732": ("AddMemberToGroup", "Success"),
    "4738": ("ChangeUserAccount", "Success"),
    "4663": ("FileAccess", "Success"),
    "5140": ("ShareAccess", None),
}

# Confirmed against AWS FSx's file-access-auditing documentation, not
# guessed.
_KEYWORDS_TO_RESULT: dict[str, str] = {
    "0x8020000000000000": "Success",
    "0x8010000000000000": "Failure",
}

FIELD_MAP_PATH = Path(__file__).parent / "ad_collector.fieldmap.json"


class ADCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        evtx_path: str,
        event_id_actions: Optional[dict[str, tuple[str, Optional[str]]]] = None,
        xml_source: Optional[Callable[[str], Iterator[str]]] = None,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._evtx_path = evtx_path
        self._event_id_actions = event_id_actions or EVENT_ID_ACTIONS
        self._xml_source = xml_source or _default_xml_source

    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        for xml_str in self._xml_source(self._evtx_path):
            record = self._parse_record(xml_str)
            if record is not None:
                yield record

    def _parse_record(self, xml_str: str) -> Optional[dict[str, Any]]:
        root = ET.fromstring(xml_str)
        system = root.find("e:System", _EVENT_NS)
        if system is None:
            return None

        event_id_el = system.find("e:EventID", _EVENT_NS)
        event_id = event_id_el.text.strip() if event_id_el is not None and event_id_el.text else None
        if event_id not in self._event_id_actions:
            return None
        action, fixed_result = self._event_id_actions[event_id]

        if fixed_result is not None:
            result = fixed_result
        else:
            keywords_el = system.find("e:Keywords", _EVENT_NS)
            keywords = keywords_el.text.strip() if keywords_el is not None and keywords_el.text else None
            result = _KEYWORDS_TO_RESULT.get(keywords, "")

        time_el = system.find("e:TimeCreated", _EVENT_NS)
        time_value = time_el.get("SystemTime") if time_el is not None else None

        computer_el = system.find("e:Computer", _EVENT_NS)
        computer = computer_el.text if computer_el is not None and computer_el.text else None

        data: dict[str, str] = {}
        event_data = root.find("e:EventData", _EVENT_NS)
        if event_data is not None:
            for d in event_data.findall("e:Data", _EVENT_NS):
                name = d.get("Name")
                if name:
                    data[name] = d.text or ""

        return {
            "TargetSid": data.get("TargetSid") or data.get("TargetUserSid"),
            "SubjectUserSid": data.get("SubjectUserSid"),
            "TargetUserName": data.get("TargetUserName"),
            "SubjectUserName": data.get("SubjectUserName"),
            "ObjectName": data.get("ObjectName"),
            "ShareName": data.get("ShareName"),
            "Computer": computer,
            "TimeCreated": time_value,
            "_resolved_action": action,
            "_resolved_result": result,
        }


def _default_xml_source(evtx_path: str) -> Iterator[str]:
    import Evtx.Evtx as evtx  # python-evtx
    with evtx.Evtx(evtx_path) as log:
        for record in log.records():
            yield record.xml()


# ---------------------------------------------------------------------------
# Reference example.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return ADCollector(
        evtx_path=config["evtx_path"],
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="active_directory_dc_eventlog",
        source_system="active_directory_dc_eventlog",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )
