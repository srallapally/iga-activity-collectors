# examples/office365_collector.py
"""
Office 365 Collector — Office 365 Management Activity API. Covers
activity *inside* M365 workloads (Exchange, SharePoint, Teams) — what a
user did with mail/files/sites, distinct from Entra's directory events
and Azure's infrastructure events.

Auth reuses TokenClient (third distinct Microsoft resource audience in
this project): OAuth2 client_credentials, scope
https://manage.office.com/.default.

This API's shape is a genuine three-step pull — ALL of it stays Python
API mechanics, none of it is field mapping:
  1. POST .../subscriptions/start?contentType=X — one-time subscription
     activation; "already subscribed" is expected and tolerated, not an
     error.
  2. GET .../subscriptions/content?contentType=X&startTime=&endTime= —
     lists content BLOB REFERENCES, not records. Time window must be
     <=24h, so poll_records() chunks since-to-now into <=24h windows.
  3. GET {contentUri} — each blob fetched separately for its actual
     record array. Real 1-to-many fan-out per /content call.

Content type defaults to Audit.Exchange (mailbox permission/delegate
changes, inbox rules) rather than Audit.AzureActiveDirectory, which would
duplicate entra_collector.py's EntraDirectoryAuditCollector.

Operation scoping (operation_names) is an inclusion decision and stays in
poll_records(), same category as AWS's event_names filter — defaults to
None (no filtering) here since I don't have confident authority to curate
a default Exchange/SharePoint operation subset.

Field mapping is declarative — see office365_collector.fieldmap.json.
Record mapping: native_user_id <- UserId, action <- Operation,
target <- MailboxOwnerUPN or Workload (fallback chain), time <-
CreationTime, outcome <- ResultStatus via the "o365_status_to_outcome"
transform (both Success/Succeeded and Failed/Failure vocabularies
handled defensively — genuine uncertainty about which O365 actually
uses, noted when this collector was first built, still unresolved).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector
from iga_collectors.uploader import TokenClient

FIELD_MAP_PATH = Path(__file__).parent / "office365_collector.fieldmap.json"


def _chunk_24h_windows(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    window = timedelta(hours=24)
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + window, end)
        yield cursor, chunk_end
        cursor = chunk_end


class Office365AuditCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        content_type: str = "Audit.Exchange",
        operation_names: Optional[frozenset[str]] = None,
        initial_lookback_seconds: Optional[int] = None,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._tenant_id = tenant_id
        self._content_type = content_type
        self._operation_names = operation_names
        self._initial_lookback_seconds = initial_lookback_seconds
        self._token_client = TokenClient(
            token_url=f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            client_id=client_id,
            client_secret=client_secret,
            scope="https://manage.office.com/.default",
            session=session,
            timeout=timeout,
        )
        self._session = session or requests.Session()
        self._timeout = timeout
        self._subscription_started = False

    def _ensure_subscription(self) -> None:
        if self._subscription_started:
            return
        headers = {"Authorization": f"Bearer {self._token_client.get_token()}"}
        url = f"https://manage.office.com/api/v1.0/{self._tenant_id}/activity/feed/subscriptions/start"
        params = {"contentType": self._content_type, "PublisherIdentifier": self._tenant_id}
        self._session.post(url, headers=headers, params=params, timeout=self._timeout)
        self._subscription_started = True

    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        self._ensure_subscription()
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

        for window_start, window_end in _chunk_24h_windows(since_dt, now):
            for content_ref in self._list_content(window_start, window_end):
                for item in self._fetch_content_blob(content_ref["contentUri"]):
                    operation = item.get("Operation")
                    if not operation:
                        continue
                    if self._operation_names is not None and operation not in self._operation_names:
                        continue
                    yield item

    def _list_content(self, start: datetime, end: datetime) -> Iterator[dict[str, Any]]:
        url = f"https://manage.office.com/api/v1.0/{self._tenant_id}/activity/feed/subscriptions/content"
        params = {
            "contentType": self._content_type,
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "PublisherIdentifier": self._tenant_id,
        }
        while url:
            headers = {"Authorization": f"Bearer {self._token_client.get_token()}"}
            response = self._session.get(url, headers=headers, params=params, timeout=self._timeout)
            response.raise_for_status()
            yield from response.json()
            url = response.headers.get("NextPageUri")
            params = None

    def _fetch_content_blob(self, content_uri: str) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._token_client.get_token()}"}
        sep = "&" if "?" in content_uri else "?"
        url = f"{content_uri}{sep}PublisherIdentifier={self._tenant_id}"
        response = self._session.get(url, headers=headers, timeout=self._timeout)
        response.raise_for_status()
        return response.json()


# ---------------------------------------------------------------------------
# Reference example: Exchange mailbox permission/delegate activity.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    import json
    operation_names = config.get("o365_operation_names")
    operation_names = frozenset(operation_names) if operation_names else None
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return Office365AuditCollector(
        tenant_id=config["o365_tenant_id"],
        client_id=config["o365_client_id"],
        client_secret=config["o365_client_secret"],
        content_type=config.get("o365_content_type", "Audit.Exchange"),
        operation_names=operation_names,
        initial_lookback_seconds=config.get("o365_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="o365_exchange_audit",
        source_system="office365",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )
