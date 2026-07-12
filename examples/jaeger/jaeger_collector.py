# examples/jaeger_collector.py
"""
Jaeger Query Collector — polls Jaeger's HTTP Query API for spans in a
time range, a genuine time-range query (unlike the OTel Kafka collector).

Endpoint: GET {query_base_url}/api/traces?service=...&start=...&end=...
Jaeger's own docs describe this v1 HTTP JSON API as "intentionally
undocumented and subject to change" — no stable OpenAPI contract despite
near-universal real-world use.

`service` is a required query parameter; one collector instance == one
Jaeger service being monitored, same shape as JDBC needing one instance
per data source.

DESIGN BOUNDARY worth being explicit about, different from every other
collector converted so far: identity_tag/target_tag/status_tag are
PER-INSTANCE CONFIGURABLE tag names — which raw span tag to read is
itself a runtime parameter, not a fixed field name. A static field map
JSON (authored once, ahead of time) can't express "read whichever tag
key this particular instance was configured with." So this resolution
stays in Python (poll_records/_span_to_record), which pre-attaches the
already-resolved user/target/result values under fixed keys; the field
map then does plain (non-configurable) path resolution against those.
Nearly everything about a span's meaning is therefore still Python here —
a genuine, structural exception, not a shortcut.

Field mapping is declarative — see jaeger_collector.fieldmap.json — but
deliberately thin given the above.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

FIELD_MAP_PATH = Path(__file__).parent / "jaeger_collector.fieldmap.json"

_OTEL_STATUS_TO_RESULT = {"OK": "Success", "ERROR": "Failure"}


def _to_micros(dt: datetime) -> int:
    return int(dt.timestamp() * 1e6)


def _otel_status_to_result(value: Any) -> str:
    if value is None:
        return ""
    return _OTEL_STATUS_TO_RESULT.get(str(value), "")


class JaegerQueryCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        query_base_url: str,
        service_name: str,
        identity_tag: str = "enduser.id",
        target_tag: Optional[str] = None,
        status_tag: str = "otel.status_code",
        lookback_seconds: Optional[int] = None,
        limit: int = 1000,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._query_base_url = query_base_url.rstrip("/")
        self._service_name = service_name
        self._identity_tag = identity_tag
        self._target_tag = target_tag
        self._status_tag = status_tag
        self._lookback_seconds = lookback_seconds
        self._limit = limit
        self._session = session or requests.Session()
        self._timeout = timeout

    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        now = datetime.now(timezone.utc)

        if since_position is not None:
            start_dt = datetime.fromisoformat(since_position)
        elif self._lookback_seconds is not None:
            start_dt = now - timedelta(seconds=self._lookback_seconds)
        else:
            raise ValueError(
                "no checkpoint exists yet and lookback_seconds is not "
                "configured; the first run needs an explicit starting point"
            )

        params = {
            "service": self._service_name,
            "start": _to_micros(start_dt),
            "end": _to_micros(now),
            "limit": self._limit,
        }
        response = self._session.get(
            f"{self._query_base_url}/api/traces", params=params, timeout=self._timeout
        )
        response.raise_for_status()
        body = response.json()

        for trace in body.get("data", []):
            processes = trace.get("processes", {})
            for span in trace.get("spans", []):
                record = self._span_to_record(span, processes)
                if record is not None:
                    yield record

    def _span_to_record(
        self, span: dict[str, Any], processes: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        tags = {t["key"]: t.get("value") for t in span.get("tags", [])}

        user = tags.get(self._identity_tag)
        if user is None:
            return None

        start_time = span.get("startTime")
        if start_time is None:
            return None

        process = processes.get(span.get("processID"), {})
        service_name = process.get("serviceName", "unknown_service")
        target = tags.get(self._target_tag) if self._target_tag else None
        target = target if target is not None else service_name

        return {
            "user": str(user),
            "action": span.get("operationName", "unknown_operation"),
            "target": str(target),
            "startTime": str(start_time),
            "_resolved_result": _otel_status_to_result(tags.get(self._status_tag)),
        }


# ---------------------------------------------------------------------------
# Reference example.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    import json
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return JaegerQueryCollector(
        query_base_url=config["jaeger_query_base_url"],
        service_name=config["jaeger_service_name"],
        lookback_seconds=config.get("jaeger_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id=f"jaeger_{config['jaeger_service_name']}",
        source_system="jaeger",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )