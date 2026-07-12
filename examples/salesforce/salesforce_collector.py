# examples/salesforce_collector.py
"""
Salesforce Collector — EventLogFile object, Login event type.

Genuinely two-step, like Office 365's content-blob pattern but simpler:
  1. SOQL query for EventLogFile metadata (LogFile is a relative URL,
     not the data itself).
  2. GET {instance_url}{LogFile} — the actual CSV content, one CSV file
     per EventLogFile row.
Both steps stay Python API mechanics in poll_records().

Auth reuses TokenClient; instance_url is an explicit config value since
Salesforce's token response also carries it and TokenClient itself stays
generic rather than special-casing that.

DESIGN BOUNDARY worth being explicit about: LOGIN_STATUS -> outcome
comparison depends on success_status_value, which is per-instance
CONFIGURABLE (not a fixed value a stateless named transform could
encode). Transforms in this framework are looked up by name from a
global registry and take no per-instance parameters, so this comparison
stays in Python (_row_to_record), which pre-attaches _resolved_outcome to
each yielded record; the field map then does plain path resolution
against that — same reasoning as Windows EventLog's event ID table and
Google Workspace's event_name_to_result table.

Timestamp parsing uses the shared "parse_salesforce_timestamp" field map
transform — genuinely different from parse_iso8601 (compact
"YYYYMMDDHHMMSS.mmm", no separators), not another Z-suffix variant.

Field mapping is declarative — see salesforce_collector.fieldmap.json.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector
from iga_collectors.uploader import TokenClient

FIELD_MAP_PATH = Path(__file__).parent / "salesforce_collector.fieldmap.json"


class SalesforceEventLogCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        instance_url: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        api_version: str,
        sf_event_type: str = "Login",
        success_status_value: str = "LOGIN_NO_ERROR",
        initial_lookback_seconds: Optional[int] = None,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._instance_url = instance_url.rstrip("/")
        self._api_version = api_version
        self._sf_event_type = sf_event_type
        self._success_status_value = success_status_value
        self._initial_lookback_seconds = initial_lookback_seconds
        self._token_client = TokenClient(
            token_url=token_url, client_id=client_id, client_secret=client_secret,
            session=session, timeout=timeout,
        )
        self._session = session or requests.Session()
        self._timeout = timeout

    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        if since_position is not None:
            since_dt = datetime.fromisoformat(since_position)
        elif self._initial_lookback_seconds is not None:
            since_dt = now - timedelta(seconds=self._initial_lookback_seconds)
        else:
            raise ValueError(
                "no checkpoint exists yet and initial_lookback_seconds is "
                "not configured; the first run needs an explicit starting point"
            )

        for log_file_row in self._query_event_log_files(since_dt):
            for row in self._fetch_log_csv(log_file_row["LogFile"]):
                yield self._row_to_record(row)

    def _query_event_log_files(self, since_dt: datetime) -> Iterator[dict[str, Any]]:
        soql = (
            f"SELECT Id,EventType,LogFile,LogDate FROM EventLogFile "
            f"WHERE EventType='{self._sf_event_type}' "
            f"AND LogDate > {since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        url = f"{self._instance_url}/services/data/v{self._api_version}/query"
        params = {"q": soql}
        while url:
            headers = {"Authorization": f"Bearer {self._token_client.get_token()}"}
            response = self._session.get(url, headers=headers, params=params, timeout=self._timeout)
            response.raise_for_status()
            body = response.json()
            yield from body.get("records", [])
            next_records_url = body.get("nextRecordsUrl")
            url = f"{self._instance_url}{next_records_url}" if next_records_url else None
            params = None

    def _fetch_log_csv(self, log_file_path: str) -> Iterator[dict[str, str]]:
        headers = {"Authorization": f"Bearer {self._token_client.get_token()}"}
        url = f"{self._instance_url}{log_file_path}"
        response = self._session.get(url, headers=headers, timeout=self._timeout)
        response.raise_for_status()
        yield from csv.DictReader(io.StringIO(response.text))

    def _row_to_record(self, row: dict[str, str]) -> dict[str, Any]:
        status = row.get("LOGIN_STATUS")
        if not status:
            resolved_outcome = ""
        elif status == self._success_status_value:
            resolved_outcome = "Success"
        else:
            resolved_outcome = "Failure"
        return {**row, "_resolved_outcome": resolved_outcome}


# ---------------------------------------------------------------------------
# Reference example.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    import json
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return SalesforceEventLogCollector(
        instance_url=config["salesforce_instance_url"],
        token_url=f"{config['salesforce_instance_url'].rstrip('/')}/services/oauth2/token",
        client_id=config["salesforce_client_id"],
        client_secret=config["salesforce_client_secret"],
        api_version=config["salesforce_api_version"],
        initial_lookback_seconds=config.get("salesforce_initial_lookback_seconds", 86400),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="salesforce_login",
        source_system="salesforce",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )
