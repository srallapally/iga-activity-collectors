# src/iga_collectors/base.py
"""
Shared infrastructure for activity collectors.

Mirrors SailPoint IdentityIQ's activity monitoring model
(https://community.sailpoint.com/t5/IdentityIQ-Wiki/Activity-monitoring-with-IdentityIQ/ta-p/71954):

  SailPoint concept                  -> This module
  ---------------------------------------------------------------
  AbstractActivityCollector          -> BaseCollector
  ActivityFieldMap (SP_* fields)     -> BaseCollector.map_to_event() (per-collector override)
  ActivityCorrelation rule           -> IdentityCorrelator
  ActivityPositionBuilder rule       -> CheckpointStore
  ActivityConditionBuilder rule      -> BaseCollector.poll() (per-collector: uses checkpoint
                                         to build a "since last position" query/filter)
  ApplicationActivity object         -> canonical ActivityLogEvent dict (activity_log_schema_v4.json)

Output of every collector is a dict conforming to activity_log_schema_v4.json.
Required top-level fields per that schema: id, schema_version, actor_global_id,
event_id, event_time, event_type, action, outcome.
"""

from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "3.0.0"  # matches activity_log_schema_v4.json "version"


# ---------------------------------------------------------------------------
# Identity correlation (SailPoint's ActivityCorrelation rule equivalent)
# ---------------------------------------------------------------------------

class IdentityCorrelator(ABC):
    """
    Resolves a source-native account identifier to the IGA identity's
    actor_global_id (UUID), matching SailPoint's correlation rule modes
    (linkIdentity / linkAttributeName+Value / identityName / etc.).

    A real implementation queries the IGA store. No such store exists yet
    in this project, so callers must supply a concrete implementation;
    PassthroughCorrelator below is a stub for local testing only.
    """

    @abstractmethod
    def correlate(self, native_id: str, source_system: str) -> Optional[str]:
        """Return the actor_global_id (UUID string) for this native account,
        or None if no identity could be resolved (uncorrelated activity)."""
        raise NotImplementedError


class PassthroughCorrelator(IdentityCorrelator):
    """
    Stub correlator for testing collectors in isolation, without a real IGA
    store. Returns the native_id unchanged. NOT valid for production use:
    actor_global_id in the canonical schema is documented as an IGA object
    reference (UUID), and a raw native_id is not that.
    """

    def correlate(self, native_id: str, source_system: str) -> Optional[str]:
        logger.warning(
            "PassthroughCorrelator used for source_system=%s native_id=%s "
            "— actor_global_id will NOT be a real IGA UUID.",
            source_system, native_id,
        )
        return native_id


class UncorrelatedActivityError(Exception):
    """Raised when an activity cannot be correlated to an identity and the
    collector is not configured to store uncorrelated activities (mirrors
    SailPoint's "Store uncorrelated activities" aggregation task option,
    which defaults off)."""


# ---------------------------------------------------------------------------
# Checkpoint store (SailPoint's ActivityPositionBuilder rule equivalent)
# ---------------------------------------------------------------------------

class CheckpointStore:
    """
    Persists the last-read position per collector to a JSON file, so a
    collector run only pulls activity since the previous run — same intent
    as SailPoint's "Enable storage of the last activity position scanned."

    One file per collector instance, keyed by collector_id. Position value
    is opaque to this class (timestamp string, DB row id, API page token,
    whatever the collector's source needs).
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, collector_id: str) -> Optional[str]:
        if not self.path.exists():
            logger.debug("checkpoint no_entry collector=%s", collector_id)
            return None
        data = json.loads(self.path.read_text())
        position = data.get(collector_id)
        if position is None:
            logger.debug("checkpoint no_entry collector=%s", collector_id)
        else:
            logger.debug("checkpoint loaded collector=%s position=%s", collector_id, position)
        return position

    def set(self, collector_id: str, position: str) -> None:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        data[collector_id] = position
        self.path.write_text(json.dumps(data, indent=2))
        logger.debug("checkpoint saved collector=%s position=%s", collector_id, position)


# ---------------------------------------------------------------------------
# Canonical event construction helpers
# ---------------------------------------------------------------------------

@dataclass
class RawActivity:
    """
    Intermediate record a collector produces after reading its source but
    before schema mapping — deliberately shaped like SailPoint's
    ApplicationActivity (action, result, target, native user id, timestamp,
    info) so per-collector field maps stay simple and auditable.
    """
    native_user_id: str
    action: str
    target: str
    event_time: datetime
    result: str  # "success" | "failure" | "unknown" (SailPoint: Success/Failure only)
    info: Optional[str] = None
    raw: Optional[dict[str, Any]] = None  # original source row/line, for raw_event_ref/debug


def new_event_shell(
    *,
    source_system: str,
    event_type: str,
    action: str,
    outcome: str,
    actor_global_id: str,
    event_time: datetime,
) -> dict[str, Any]:
    """
    Build the required top-level fields of an ActivityLogEvent. Callers add
    optional blocks (actor, resource, ingest_metadata, ...) on top.
    """
    if outcome not in {"success", "failure", "partial", "unknown"}:
        raise ValueError(f"invalid outcome: {outcome!r}")
    if event_time.tzinfo is None:
        raise ValueError(
            "event_time must be timezone-aware; naive datetimes cannot be "
            "safely converted to UTC without guessing the source timezone. "
            "The collector's map_to_event() must attach tzinfo explicitly."
        )

    now = datetime.now(timezone.utc)
    event_id = str(uuid.uuid4())
    return {
        "id": event_id,
        "schema_version": SCHEMA_VERSION,
        "actor_global_id": actor_global_id,
        "event_id": event_id,
        "event_time": event_time.astimezone(timezone.utc).isoformat(),
        "event_type": event_type,
        "action": action,
        "outcome": outcome,
        "ingest_metadata": {
            "source_system": source_system,
            "ingest_time": now.isoformat(),
        },
    }


# SailPoint Result -> canonical outcome
RESULT_TO_OUTCOME = {
    "success": "success",
    "failure": "failure",
}


def sailpoint_result_to_outcome(result: str) -> str:
    return RESULT_TO_OUTCOME.get(result.strip().lower(), "unknown")


# ---------------------------------------------------------------------------
# Base collector
# ---------------------------------------------------------------------------

class BaseCollector(ABC):
    """
    Equivalent of sailpoint.activity.AbstractActivityCollector.

    Subclasses implement poll() to read from their specific source (JDBC,
    log file, cloud API, ...) and map_to_event() to convert one RawActivity
    into a canonical ActivityLogEvent dict. run() orchestrates polling,
    correlation, mapping, and checkpoint advancement — a subclass should not
    need to override run().
    """

    def __init__(
        self,
        collector_id: str,
        source_system: str,
        correlator: IdentityCorrelator,
        checkpoint_store: CheckpointStore,
        store_uncorrelated: bool = False,
    ):
        self.collector_id = collector_id
        self.source_system = source_system
        self.correlator = correlator
        self.checkpoint_store = checkpoint_store
        self.store_uncorrelated = store_uncorrelated
        # Set by discovery.run_all() after factory instantiation; None means
        # "inherit the process-level LOG_LEVEL / LOG_FORMAT setting".
        self.log_level: Optional[str] = None

    @abstractmethod
    def poll(self, since_position: Optional[str]) -> Iterator[RawActivity]:
        """Yield RawActivity records newer than since_position (opaque
        checkpoint value, None on first run). Equivalent to a JDBC/Log File
        collector applying its ConditionBuilder-derived filter."""
        raise NotImplementedError

    @abstractmethod
    def next_position(self, activity: RawActivity) -> str:
        """Given the most recently yielded RawActivity, return the
        checkpoint value to persist (e.g. its timestamp)."""
        raise NotImplementedError

    @abstractmethod
    def map_to_event(self, activity: RawActivity, actor_global_id: str) -> dict[str, Any]:
        """Convert one RawActivity + resolved identity into a canonical
        ActivityLogEvent dict."""
        raise NotImplementedError

    def run(self) -> Iterator[dict[str, Any]]:
        import json as _json
        import time as _time

        # Push per-collector log level for this run, restore on exit.
        root_logger = logging.getLogger("iga_collectors")
        _saved_level = root_logger.level
        if self.log_level:
            numeric = logging.getLevelName(self.log_level.upper())
            if isinstance(numeric, int):
                root_logger.setLevel(numeric)

        try:
            since = self.checkpoint_store.get(self.collector_id)
            logger.info(
                "collector starting collector=%s since=%s",
                self.collector_id, since or "beginning",
            )
            t0 = _time.monotonic()
            last_position = since
            event_count = 0
            for activity in self.poll(since):
                actor_global_id = self.correlator.correlate(
                    activity.native_user_id, self.source_system
                )
                if actor_global_id is None:
                    if not self.store_uncorrelated:
                        logger.info(
                            "record dropped uncorrelated collector=%s native_user_id=%s",
                            self.collector_id, activity.native_user_id,
                        )
                        last_position = self.next_position(activity)
                        continue
                    raise UncorrelatedActivityError(
                        f"no identity for native_user_id={activity.native_user_id!r} "
                        f"in source_system={self.source_system!r}"
                    )
                logger.debug(
                    "correlation collector=%s native_id=%s global_id=%s",
                    self.collector_id, activity.native_user_id, actor_global_id,
                )
                event = self.map_to_event(activity, actor_global_id)
                if event is None:
                    logger.debug(
                        "record dropped map_to_event returned None collector=%s",
                        self.collector_id,
                    )
                    last_position = self.next_position(activity)
                    continue
                logger.debug(
                    "mapped event collector=%s event=%s",
                    self.collector_id, _json.dumps(event, default=str),
                )
                yield event
                event_count += 1
                last_position = self.next_position(activity)

            if last_position != since:
                self.checkpoint_store.set(self.collector_id, last_position)

            logger.info(
                "collector complete collector=%s events=%d duration_s=%.1f",
                self.collector_id, event_count, _time.monotonic() - t0,
            )
        finally:
            root_logger.setLevel(_saved_level)


# ---------------------------------------------------------------------------
# Tabular field-mapped collector (shared shape for JDBC / Log File / RACF)
# ---------------------------------------------------------------------------
#
# SailPoint's JDBC, Log File, and RACF Audit Log collectors all populate the
# same ApplicationActivity shape via a FieldMap (SP_Action, SP_NativeUserId,
# SP_Target, SP_TimeStamp required; SP_Result optional), checkpointed by the
# last record's timestamp. Only how each one gets the raw field values
# differs (SQL row / regex match / fixed-width slice). This class shares
# the field-extraction, outcome-mapping, and canonical-event-construction
# logic; concrete subclasses implement only source access.
#
# This does NOT fit sources with non-timestamp pagination (e.g. an API
# cursor token) — those subclass BaseCollector directly instead of forcing
# a mismatched shape onto this class.

@dataclass(frozen=True)
class ColumnMap:
    """Maps named raw fields to RawActivity fields. Field names are
    matched case-insensitively. `result` is optional, matching SailPoint's
    own FieldMap (SP_Result is not in its required-fields list)."""
    native_user_id: str
    action: str
    target: str
    time: str
    result: Optional[str] = None


def lookup_field(values: dict[str, Any], field_name: str) -> Any:
    key = field_name.upper()
    normalized = {k.upper(): v for k, v in values.items()}
    if key not in normalized:
        raise KeyError(
            f"field {field_name!r} not found in record; available "
            f"fields: {sorted(values)}"
        )
    return normalized[key]


class TabularActivityCollector(BaseCollector):
    def __init__(
        self,
        *,
        column_map: ColumnMap,
        source_timezone: timezone,
        time_parser: Optional[Callable[[Any], datetime]] = None,
        event_type: str = "resource_access",
        outcome_mapper: Callable[[str], str] = sailpoint_result_to_outcome,
        default_outcome: str = "unknown",
        **base_kwargs: Any,
    ):
        """
        time_parser: converts a raw time field value to a datetime.
        Defaults to None, meaning the raw value is assumed to already be a
        datetime (true for JDBC drivers, which type-convert TIMESTAMP
        columns automatically). Sources that yield timestamps as strings
        (log files, fixed-width records) must supply a parser.
        """
        super().__init__(**base_kwargs)
        self._column_map = column_map
        self._source_timezone = source_timezone
        self._time_parser = time_parser
        self._event_type = event_type
        self._outcome_mapper = outcome_mapper
        self._default_outcome = default_outcome

    @abstractmethod
    def poll_rows(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        """Yield raw field-value dicts newer than since_position. Concrete
        subclasses implement source access here instead of poll()."""
        raise NotImplementedError

    def poll(self, since_position: Optional[str]) -> Iterator[RawActivity]:
        for values in self.poll_rows(since_position):
            yield self._values_to_activity(values)

    def _values_to_activity(self, values: dict[str, Any]) -> RawActivity:
        cm = self._column_map

        raw_time = lookup_field(values, cm.time)
        parsed_time = self._time_parser(raw_time) if self._time_parser else raw_time
        if not isinstance(parsed_time, datetime):
            raise TypeError(
                f"field {cm.time!r} did not resolve to a datetime "
                f"(got {type(parsed_time)!r}); supply a time_parser if "
                f"this source yields timestamps as strings or another type"
            )
        if parsed_time.tzinfo is None:
            parsed_time = parsed_time.replace(tzinfo=self._source_timezone)

        result_value = str(lookup_field(values, cm.result)) if cm.result else ""

        return RawActivity(
            native_user_id=str(lookup_field(values, cm.native_user_id)),
            action=str(lookup_field(values, cm.action)),
            target=str(lookup_field(values, cm.target)),
            event_time=parsed_time,
            result=result_value,
            raw=values,
        )

    def next_position(self, activity: RawActivity) -> str:
        return activity.event_time.isoformat()

    def map_to_event(self, activity: RawActivity, actor_global_id: str) -> dict[str, Any]:
        outcome = (
            self._outcome_mapper(activity.result) if activity.result else self._default_outcome
        )
        event = new_event_shell(
            source_system=self.source_system,
            event_type=self._event_type,
            action=activity.action,
            outcome=outcome,
            actor_global_id=actor_global_id,
            event_time=activity.event_time,
        )
        event["resource"] = {"resource_name": activity.target}
        return event