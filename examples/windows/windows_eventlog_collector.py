# examples/windows_eventlog_collector.py
"""
Two real Python options exist for reading Windows event data
at all, and they are different collection models, not interchangeable:

  1. `python-evtx` (https://pypi.org/project/python-evtx/) — pure Python,
     cross-platform, parses exported .evtx files. No live connection; you
     export/ship the .evtx file to wherever this collector runs.
  2. `pywin32`'s `win32evtlog` — live connection to a local or remote
     Windows host's event log via the Windows API. Windows-only (or
     requires running near/on the target host).

This collector implements option 1 (python-evtx against .evtx files),
since it doesn't require running on Windows or maintaining a live
connection — closer in deployment shape to the other file/DB-based
collectors here. `pip install python-evtx` before use.

Scope: Windows Security log has ~500 possible event IDs. Per this
project's "focus is on user (account) related activity," this collector
only recognizes a specific, well-known set of account-lifecycle and
authentication event IDs (EVENT_ID_ACTIONS below) — logon, logoff, and
account create/delete/modify/group-membership events. Events outside this
set are skipped, not errored, since a raw Security log is overwhelmingly
non-account noise (privilege use, object access, process creation, etc.)
that isn't the target of this project. Extend EVENT_ID_ACTIONS if you need
more.

Field extraction, outcome mapping, and canonical event construction are
shared with the JDBC and Log File collectors via
iga_collectors.base.TabularActivityCollector. Unlike those sources,
Windows Security events don't carry an explicit "result" field alongside
the record — the event ID itself encodes success/failure (4624 vs 4625),
so EVENT_ID_ACTIONS supplies both the action name and the result string
per event ID, matching the outcome vocabulary TabularActivityCollector
already expects (Success/Failure via sailpoint_result_to_outcome).

Target user extraction: TargetUserName (the account being acted on) is
preferred; SubjectUserName (the acting process's identity, often SYSTEM
for local logon flows) is used only as a fallback when TargetUserName is
absent — this matches how these fields are actually populated across the
event IDs in scope.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from iga_collectors.base import (
    CheckpointStore,
    ColumnMap,
    IdentityCorrelator,
    PassthroughCorrelator,
    TabularActivityCollector,
    lookup_field,
)

_EVENT_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}

# event_id -> (action, result). Scoped to account/authentication events
# per this project's focus; see module docstring.
EVENT_ID_ACTIONS: dict[str, tuple[str, str]] = {
    "4624": ("Login", "Success"),
    "4625": ("Login", "Failure"),
    "4634": ("Logoff", "Success"),
    "4720": ("CreateUser", "Success"),
    "4726": ("DeleteUser", "Success"),
    "4732": ("AddMemberToGroup", "Success"),
    "4738": ("ChangeUserAccount", "Success"),
}


def parse_windows_timestamp(value: str) -> datetime:
    """Parses a Windows EventLog TimeCreated SystemTime value (e.g.
    '2026-07-10T10:44:47.1234567Z') into a tz-aware datetime. Truncates
    fractional seconds to 6 digits — Windows often supplies 7, more
    precision than Python's datetime supports."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    m = re.match(r"^(.*\.\d{6})\d*(\+00:00)$", v)
    if m:
        v = m.group(1) + m.group(2)
    return datetime.fromisoformat(v)


class WindowsEventLogCollector(TabularActivityCollector):
    def __init__(
        self,
        *,
        evtx_path: str,
        event_id_actions: Optional[dict[str, tuple[str, str]]] = None,
        xml_source: Optional[Callable[[str], Iterator[str]]] = None,
        **tabular_kwargs: Any,
    ):
        super().__init__(**tabular_kwargs)
        self._evtx_path = evtx_path
        self._event_id_actions = event_id_actions or EVENT_ID_ACTIONS
        self._xml_source = xml_source or _default_xml_source

    def poll_rows(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        since_dt = datetime.fromisoformat(since_position) if since_position else None
        cm = self._column_map

        for xml_str in self._xml_source(self._evtx_path):
            values = self._parse_record(xml_str)
            if values is None:
                continue
            parsed_time = self._parse_time(lookup_field(values, cm.time))
            if since_dt is not None and parsed_time <= since_dt:
                continue
            yield values

    def _parse_record(self, xml_str: str) -> Optional[dict[str, str]]:
        root = ET.fromstring(xml_str)
        system = root.find("e:System", _EVENT_NS)
        if system is None:
            return None

        event_id_el = system.find("e:EventID", _EVENT_NS)
        event_id = event_id_el.text.strip() if event_id_el is not None and event_id_el.text else None
        if event_id not in self._event_id_actions:
            return None
        action, result = self._event_id_actions[event_id]

        time_el = system.find("e:TimeCreated", _EVENT_NS)
        time_value = time_el.get("SystemTime") if time_el is not None else None
        if not time_value:
            return None

        computer_el = system.find("e:Computer", _EVENT_NS)
        computer = computer_el.text if computer_el is not None and computer_el.text else "unknown"

        data: dict[str, str] = {}
        event_data = root.find("e:EventData", _EVENT_NS)
        if event_data is not None:
            for d in event_data.findall("e:Data", _EVENT_NS):
                name = d.get("Name")
                if name:
                    data[name] = d.text or ""

        # SID is the immutable account identifier; fall back to name when absent.
        sid = (
            data.get("TargetSid") or data.get("TargetUserSid")
            or data.get("SubjectUserSid")
        )
        name = data.get("TargetUserName") or data.get("SubjectUserName")
        actor_id = sid or name
        if not actor_id:
            return None

        return {
            "actor_id": actor_id,
            "action": action,
            "target": computer,
            "time": time_value,
            "result": result,
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
    return WindowsEventLogCollector(
        evtx_path=config["evtx_path"],
        column_map=ColumnMap(
            native_user_id="actor_id", action="action", target="target",
            time="time", result="result",
        ),
        source_timezone=timezone.utc,
        time_parser=parse_windows_timestamp,
        collector_id="windows_security_eventlog",
        source_system="windows_security_eventlog",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )