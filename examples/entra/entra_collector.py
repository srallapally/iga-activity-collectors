# examples/entra_collector.py
"""
Microsoft Entra ID Collector — Microsoft Graph auditLogs API.

Uses `requests` directly against the Graph REST API rather than the
official `msgraph-sdk` Python package (async-first; no real benefit here
over plain REST + this framework's existing OAuth2 client).

Auth reuses iga_collectors.uploader.TokenClient — Entra's app-only auth is
the same OAuth2 client_credentials grant, pointed at Microsoft's token
endpoint with scope https://graph.microsoft.com/.default.

Two distinct Graph resources are both "Entra ID activity" but different
endpoints with different shapes:
  - /auditLogs/signIns        — authentication events
  - /auditLogs/directoryAudits — account/group/app lifecycle changes

Per this project's "focus is on user (account) related activity",
create_collector() wires up EntraDirectoryAuditCollector by default,
loading entra_collector.fieldmap.json. EntraSignInCollector is also
implemented, using its own entra_collector.signin.fieldmap.json — not
wired to the default factory, since directoryAudits is the primary use
case here; instantiate it yourself (with its own collector_id/checkpoint,
since it's a distinct data source) if sign-in telemetry is also wanted.

Field mapping is declarative for both classes — see the two .fieldmap.json
files. This module's own job is now purely Graph API mechanics: OAuth2
token handling, @odata.nextLink pagination. poll_records() yields raw
Graph API items completely unmodified.

Timestamp parsing reuses the shared "parse_iso8601" field map transform
(iga_collectors.field_mapping) rather than a local copy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector
from iga_collectors.uploader import TokenClient

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

FIELD_MAP_PATH = Path(__file__).parent / "entra_collector.fieldmap.json"
SIGNIN_FIELD_MAP_PATH = Path(__file__).parent / "entra_collector.signin.fieldmap.json"


class _GraphClient:
    """Shared OAuth2 token handling and paginated GET for Microsoft Graph."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
    ):
        self._token_client = TokenClient(
            token_url=f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            client_id=client_id,
            client_secret=client_secret,
            scope="https://graph.microsoft.com/.default",
            session=session,
            timeout=timeout,
        )
        self._session = session or requests.Session()
        self._timeout = timeout

    def get_pages(self, url: str, params: Optional[dict[str, Any]] = None) -> Iterator[dict[str, Any]]:
        """Yields individual items across all pages, following @odata.nextLink."""
        while url:
            headers = {"Authorization": f"Bearer {self._token_client.get_token()}"}
            response = self._session.get(url, headers=headers, params=params, timeout=self._timeout)
            response.raise_for_status()
            body = response.json()
            yield from body.get("value", [])
            url = body.get("@odata.nextLink")
            params = None  # nextLink already carries the query string


class _EntraGraphCollectorBase(DeclarativeMappedCollector):
    """Shared "since checkpoint or initial lookback, else error" pagination
    entry point for both Graph audit endpoints below."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        initial_lookback_seconds: Optional[int] = None,
        page_size: int = 100,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._graph = _GraphClient(tenant_id, client_id, client_secret, session, timeout)
        self._initial_lookback_seconds = initial_lookback_seconds
        self._page_size = page_size

    def _resolve_since_dt(self, since_position: Optional[str]) -> datetime:
        if since_position is not None:
            return datetime.fromisoformat(since_position)
        if self._initial_lookback_seconds is not None:
            return datetime.now(timezone.utc) - timedelta(seconds=self._initial_lookback_seconds)
        raise ValueError(
            "no checkpoint exists yet and initial_lookback_seconds is not "
            "configured; the first run needs an explicit starting point"
        )


class EntraSignInCollector(_EntraGraphCollectorBase):
    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        since_dt = self._resolve_since_dt(since_position)
        filter_str = f"createdDateTime ge {since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        url = f"{GRAPH_BASE_URL}/auditLogs/signIns"
        params = {"$filter": filter_str, "$top": self._page_size, "$orderby": "createdDateTime asc"}
        yield from self._graph.get_pages(url, params)


class EntraDirectoryAuditCollector(_EntraGraphCollectorBase):
    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        since_dt = self._resolve_since_dt(since_position)
        filter_str = f"activityDateTime ge {since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        url = f"{GRAPH_BASE_URL}/auditLogs/directoryAudits"
        params = {"$filter": filter_str, "$top": self._page_size, "$orderby": "activityDateTime asc"}
        yield from self._graph.get_pages(url, params)


# ---------------------------------------------------------------------------
# Reference example: directoryAudits (account/group/app lifecycle events).
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    import json
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return EntraDirectoryAuditCollector(
        tenant_id=config["entra_tenant_id"],
        client_id=config["entra_client_id"],
        client_secret=config["entra_client_secret"],
        initial_lookback_seconds=config.get("entra_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="entra_directory_audits",
        source_system="entra_id",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )
