# src/iga_collectors/field_mapping.py
"""
Declarative field mapping: turns one already-fetched raw record (a dict)
into a canonical ActivityLogEvent, driven by a JSON document instead of
per-collector Python extraction code.

Deliberately scoped: this replaces the RECORD-LEVEL mapping only --
"raw field X becomes canonical field Y, optionally transformed." It does
NOT replace API mechanics (auth, pagination, multi-step flows like
Office 365's subscribe/list/fetch or Salesforce's query/blob-fetch) --
those stay as Python in each collector's poll_records(). A JSON document
can reasonably say "EventName maps to action"; it can't reasonably say
"POST here, follow this cursor, decode this base64 blob" without turning
into a bespoke API-connector DSL, which is a materially bigger project
than what was asked for.

Field map document shape:

    {
      "source_system": "aws_cloudtrail",
      "event_type": "resource_access",
      "fields": {
        "_native_actor_id": {"source_path": "Username", "required": true},
        "_event_time": {"source_path": "EventTime", "required": true},
        "action": {"source_path": "EventName", "required": true},
        "outcome": {
          "source_path": "CloudTrailEvent.$json",
          "transform": "error_code_presence_to_outcome",
          "default": "unknown"
        },
        "resource.resource_name": {
          "source_path": ["Resources[0].ResourceName", "EventSource"],
          "default": "unknown_resource"
        }
      }
    }

Two field names are reserved and handled specially, not written literally
into the event: "_native_actor_id" (fed to the collector's
IdentityCorrelator) and "_event_time" (must resolve to a datetime; drives
both the event's timestamp and the checkpoint). Every other key is a
dotted canonical schema path (e.g. "environment.source_ip") written
directly into the nested event dict.

source_path syntax: dot-separated segments. "a.b[0].c" indexes into a
list. A literal "$json" segment means "the current value is a JSON-
encoded string; parse it and continue from there" -- needed for sources
like CloudTrail where a whole sub-object arrives pre-serialized inside
one string field. source_path may be a single string or a list of
strings tried in order (first non-None wins) -- for "try this field,
fall back to that one" patterns.

A field entry may use "literal" instead of "source_path" for a fixed
value with no record lookup. "transform" names a function from
FIELD_TRANSFORMS below, applied to the resolved value. "default" supplies
a value when the resolved (and transformed) value is None. "required":
true means the whole record is dropped (mapping returns None) if the
final value is still None.
"""

from __future__ import annotations

import json
import logging
import re
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

from iga_collectors.base import BaseCollector, RawActivity, new_event_shell, sailpoint_result_to_outcome

logger = logging.getLogger(__name__)

_INDEX_SEGMENT = re.compile(r"^(\w+)\[(\d+)\]$")


def resolve_path(record: Any, path: str) -> Any:
    """Walk a dotted path (with optional [N] indexing and a special
    "$json" parse-this-string-then-continue segment) into record. Returns
    None anywhere the path doesn't resolve, rather than raising -- a
    missing field is an ordinary, expected outcome here, not an error."""
    current = record
    for segment in path.split("."):
        if current is None:
            return None

        if segment == "$json":
            if not isinstance(current, str):
                return None
            try:
                current = json.loads(current)
            except (TypeError, ValueError):
                return None
            continue

        match = _INDEX_SEGMENT.match(segment)
        if match:
            key, index = match.group(1), int(match.group(2))
            if not isinstance(current, dict) or key not in current:
                return None
            sequence = current[key]
            if not isinstance(sequence, list) or index >= len(sequence):
                return None
            current = sequence[index]
        else:
            if not isinstance(current, dict) or segment not in current:
                return None
            current = current[segment]

    return current


def _transform_identity(value: Any) -> Any:
    return value


def _transform_error_code_presence_to_outcome(value: Any) -> Optional[str]:
    """For sources (like CloudTrail) whose only failure signal is the
    presence of an errorCode-shaped field on an otherwise-optional
    embedded detail object. None input (no detail at all) stays None --
    "we have no evidence" is different from "we have evidence of
    success" and callers should use "default" to pick the outcome for
    that case, not have this transform assert one."""
    if not isinstance(value, dict):
        return None
    return "failure" if value.get("errorCode") else "success"


def _transform_access_key_to_credential_type(value: Any) -> Optional[str]:
    """Maps the presence of an access key ID to the credential_type
    enum value api_key; absent -> None (field omitted), not a guess at
    some other credential type."""
    return "api_key" if value else None


def _transform_sailpoint_result_to_outcome(value: Any) -> Optional[str]:
    """Wraps base.sailpoint_result_to_outcome (which calls .strip() and
    would raise on non-string input) so it's safe to use directly as a
    field map transform against a possibly-missing raw value."""
    if not isinstance(value, str):
        return None
    return sailpoint_result_to_outcome(value)


def _transform_parse_iso8601(value: Any) -> Optional[datetime]:
    """Handles a trailing 'Z' (datetime.fromisoformat doesn't accept it
    before Python 3.11) and truncates fractional seconds beyond 6 digits
    (several APIs in this project return 7). This is the sixth place
    this exact handling was needed (Windows, Entra, Azure, O365, Google
    Workspace each had their own copy) -- the field map transform
    registry is the right place for it to stop being duplicated."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    m = re.match(r"^(.*\.\d{6})\d*([+-]\d{2}:\d{2})$", v)
    if m:
        v = m.group(1) + m.group(2)
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _transform_bool_to_outcome(value: Any) -> Optional[str]:
    """For sources reporting success as a plain boolean rather than a
    string or a nested detail object."""
    if not isinstance(value, bool):
        return None
    return "success" if value else "failure"


def _transform_graph_signin_error_code_to_outcome(value: Any) -> str:
    """Microsoft Graph signIns convention: errorCode == 0 means success;
    anything else, including the field being entirely absent (None),
    means failure. Deliberately never returns None -- this matches the
    original imperative implementation's behavior exactly, including
    treating "no errorCode field at all" the same as "a nonzero one."""
    return "success" if value == 0 else "failure"


_AZURE_STATUS_TO_OUTCOME = {"succeeded": "success", "failed": "failure"}


def _transform_azure_status_to_outcome(value: Any) -> Optional[str]:
    """Azure Activity Log's status.value uses its own vocabulary
    ("Succeeded"/"Failed"/"Started"/etc), not the Success/Failure strings
    sailpoint_result_to_outcome expects. "Started" and anything else
    in-flight/unrecognized returns None, so the field map's own "default"
    (usually "unknown") decides that case rather than this transform
    guessing."""
    if not value:
        return None
    return _AZURE_STATUS_TO_OUTCOME.get(str(value).strip().lower())


_O365_STATUS_TO_OUTCOME = {
    "success": "success", "succeeded": "success",
    "failed": "failure", "failure": "failure",
}


def _transform_o365_status_to_outcome(value: Any) -> Optional[str]:
    """O365 audit records' ResultStatus vocabulary wasn't confirmed via
    search to be consistent across workloads (Success/Succeeded and
    Failed/Failure both plausible), so both forms are mapped defensively
    -- same genuine uncertainty noted when this collector was first
    built, not resolved since."""
    if not value:
        return None
    return _O365_STATUS_TO_OUTCOME.get(str(value).strip().lower())


def _transform_parse_salesforce_timestamp(value: Any) -> Optional[datetime]:
    """Salesforce EventLogFile CSV TIMESTAMP format is compact
    "YYYYMMDDHHMMSS.mmm" (e.g. "20140929224335.668"), UTC implied, no
    separators at all -- not interchangeable with parse_iso8601."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    date_part, _, frac = value.strip().partition(".")
    try:
        dt = datetime.strptime(date_part, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    if frac:
        dt = dt.replace(microsecond=int(frac.ljust(6, "0")[:6]))
    return dt.replace(tzinfo=timezone.utc)


def _transform_gcp_status_code_to_outcome(value: Any) -> str:
    """google.rpc.Status convention: code absent or 0 means OK/success;
    any other value means failure. Always returns a string, never None --
    matches the original imperative implementation, which never produced
    an "unknown" outcome for this source."""
    return "failure" if value else "success"


def _transform_unix_nanos_to_datetime(value: Any) -> Optional[datetime]:
    """OTLP JSON encodes timestamps as a string of nanoseconds since the
    Unix epoch (protobuf JSON int64 convention)."""
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


_OTEL_STATUS_CODE_TO_OUTCOME = {
    1: "success", "STATUS_CODE_OK": "success",
    2: "failure", "STATUS_CODE_ERROR": "failure",
}


def _transform_otel_status_code_to_outcome(value: Any) -> Optional[str]:
    """OTLP span status.code: may arrive as an int (1/2) or the string
    enum name, depending on the exporter -- both forms are mapped.
    Unset/absent (None) or code 0 (STATUS_CODE_UNSET) returns None, so
    the field map's own "default" (usually "unknown") decides that case."""
    if value is None:
        return None
    return _OTEL_STATUS_CODE_TO_OUTCOME.get(value)


def _transform_micros_to_datetime(value: Any) -> Optional[datetime]:
    """Jaeger's startTime is microseconds since the Unix epoch -- distinct
    from raw OTLP JSON's nanoseconds (unix_nanos_to_datetime)."""
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1e6, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


FIELD_TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "identity": _transform_identity,
    "error_code_presence_to_outcome": _transform_error_code_presence_to_outcome,
    "access_key_to_credential_type": _transform_access_key_to_credential_type,
    "sailpoint_result_to_outcome": _transform_sailpoint_result_to_outcome,
    "parse_iso8601": _transform_parse_iso8601,
    "bool_to_outcome": _transform_bool_to_outcome,
    "graph_signin_error_code_to_outcome": _transform_graph_signin_error_code_to_outcome,
    "azure_status_to_outcome": _transform_azure_status_to_outcome,
    "o365_status_to_outcome": _transform_o365_status_to_outcome,
    "parse_salesforce_timestamp": _transform_parse_salesforce_timestamp,
    "gcp_status_code_to_outcome": _transform_gcp_status_code_to_outcome,
    "unix_nanos_to_datetime": _transform_unix_nanos_to_datetime,
    "otel_status_code_to_outcome": _transform_otel_status_code_to_outcome,
    "micros_to_datetime": _transform_micros_to_datetime,
}


class FieldMapError(Exception):
    pass


def _resolve_field(record: dict[str, Any], spec: dict[str, Any]) -> Any:
    if "literal" in spec:
        value = spec["literal"]
    else:
        paths = spec.get("source_path")
        if paths is None:
            raise FieldMapError(f"field spec has neither 'literal' nor 'source_path': {spec!r}")
        if isinstance(paths, str):
            paths = [paths]
        value = None
        for path in paths:
            value = resolve_path(record, path)
            if value is not None:
                break

    transform_name = spec.get("transform")
    if transform_name:
        transform = FIELD_TRANSFORMS.get(transform_name)
        if transform is None:
            raise FieldMapError(f"unknown transform: {transform_name!r}")
        value = transform(value)

    if value is None and "default" in spec:
        value = spec["default"]

    return value


def set_dotted(target: dict[str, Any], path: str, value: Any) -> None:
    """Set target["a"]["b"]["c"] = value for path "a.b.c", creating
    intermediate dicts as needed."""
    parts = path.split(".")
    node = target
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class DeclarativeMappedCollector(BaseCollector):
    """
    Like TabularActivityCollector, but the raw-record-to-canonical-event
    mapping is driven by a loaded field map JSON document (see module
    docstring) instead of per-collector Python code. Concrete subclasses
    implement only poll_records() -- API mechanics: auth, pagination,
    which records to include. Everything about what those records'
    fields mean is data (the field map), not code.
    """

    def __init__(
        self,
        *,
        field_map: dict[str, Any],
        source_timezone: timezone,
        filter_by_checkpoint: bool = True,
        **base_kwargs: Any,
    ):
        """
        filter_by_checkpoint: when True (the default), poll() drops any
        record whose resolved _event_time is at or before the last
        checkpoint -- correct for sources where resumption genuinely
        means "give me everything newer than X" (every REST-polled
        source in this project).

        Set False for sources whose resumption is handled by something
        OTHER than this timestamp checkpoint -- e.g. a Kafka consumer's
        own committed offset (see otel_collector.py). For those sources,
        applying this filter anyway is a real bug, not a redundant
        safety net: distributed tracing has legitimate clock skew across
        services, so a span's timestamp can land at or before the
        checkpoint without being a duplicate, and the filter would
        silently drop valid data.
        """
        super().__init__(**base_kwargs)
        self._field_map = field_map
        self._fields: dict[str, dict[str, Any]] = field_map["fields"]
        self._mapped_source_system = field_map.get("source_system", self.source_system)
        self._mapped_event_type = field_map.get("event_type", "resource_access")
        self._source_timezone = source_timezone
        self._filter_by_checkpoint = filter_by_checkpoint

    @abstractmethod
    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        """Yield raw record dicts newer than since_position. API
        mechanics (auth, pagination, which records to include) belong
        here; field-level mapping does not -- that's driven by the field
        map document instead."""
        raise NotImplementedError

    def poll(self, since_position: Optional[str]) -> Iterator[RawActivity]:
        since_dt = (
            datetime.fromisoformat(since_position)
            if since_position and self._filter_by_checkpoint
            else None
        )
        yielded = 0
        for record in self.poll_records(since_position):
            activity = self._record_to_activity(record)
            if activity is None:
                continue
            if since_dt is not None and activity.event_time <= since_dt:
                continue
            yielded += 1
            yield activity
        logger.debug(
            "poll complete collector=%s records_yielded=%d since=%s",
            self.collector_id, yielded, since_position or "beginning",
        )

    def _record_to_activity(self, record: dict[str, Any]) -> Optional[RawActivity]:
        resolved: dict[str, Any] = {}
        for path, spec in self._fields.items():
            value = _resolve_field(record, spec)
            if value is None and spec.get("required"):
                logger.debug(
                    "record dropped required field missing collector=%s field=%s",
                    self.collector_id, path,
                )
                return None
            resolved[path] = value

        native_actor_id = resolved.pop("_native_actor_id", None)
        event_time = resolved.pop("_event_time", None)
        if native_actor_id is None or event_time is None:
            return None
        if not isinstance(event_time, datetime):
            raise FieldMapError(
                f"_event_time resolved to {type(event_time)!r}, not a datetime; "
                f"check the field map's transform for _event_time"
            )
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=self._source_timezone)

        canonical_fields = {k: v for k, v in resolved.items() if v is not None}
        return RawActivity(
            native_user_id=str(native_actor_id),
            action=str(canonical_fields.get("action", "")),
            target=str(canonical_fields.get("resource.resource_name", "")),
            event_time=event_time,
            result="",
            raw=canonical_fields,
        )

    def next_position(self, activity: RawActivity) -> str:
        return activity.event_time.isoformat()

    def map_to_event(self, activity: RawActivity, actor_global_id: str) -> dict[str, Any]:
        canonical_fields = dict(activity.raw or {})
        outcome = canonical_fields.pop("outcome", None) or "unknown"
        action = canonical_fields.pop("action", "")

        event = new_event_shell(
            source_system=self._mapped_source_system,
            event_type=self._mapped_event_type,
            action=action,
            outcome=outcome,
            actor_global_id=actor_global_id,
            event_time=activity.event_time,
        )
        for path, value in canonical_fields.items():
            set_dotted(event, path, value)
        return event