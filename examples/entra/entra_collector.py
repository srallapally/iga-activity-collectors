# examples/entra/entra_collector.py
"""
Microsoft Entra ID Collector — Microsoft Graph auditLogs API.

Uses `requests` directly against the Graph REST API rather than the
official `msgraph-sdk` Python package (async-first; no real benefit here
over plain REST + this framework's existing OAuth2 client).

Auth reuses iga_collectors.uploader.TokenClient — Entra's app-only auth is
the same OAuth2 client_credentials grant, pointed at Microsoft's token
endpoint with scope https://graph.microsoft.com/.default.

Three distinct Graph resources are all "Entra ID activity" but different
endpoints with different shapes:
  - /auditLogs/directoryAudits          — account/group/app lifecycle changes
  - /auditLogs/signIns                  — interactive and non-interactive user sign-ins
  - /auditLogs/servicePrincipalSignIns  — service principal sign-ins (daemon/app OAuth2 flows)

create_collector() returns EntraCombinedCollector, which runs all three
sub-streams in sequence under a single COLLECTORS_DIR entry. Each stream
keeps its own checkpoint key so they advance independently — a failure in
one stream does not affect the others' checkpoints.

Field mapping is declarative for all three streams:
  entra_collector.fieldmap.json           — directoryAudits
  entra_collector.signin.fieldmap.json    — signIns
  entra_collector.sp_signin.fieldmap.json — servicePrincipalSignIns

Required Graph API permissions (application, with admin consent):
  AuditLog.Read.All
  Directory.Read.All
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

from iga_collectors.base import BaseCollector, CheckpointStore, PassthroughCorrelator, RawActivity
from iga_collectors.field_mapping import DeclarativeMappedCollector
from iga_collectors.uploader import TokenClient

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

FIELD_MAP_PATH = Path(__file__).parent / "entra_collector.fieldmap.json"
SIGNIN_FIELD_MAP_PATH = Path(__file__).parent / "entra_collector.signin.fieldmap.json"
SP_SIGNIN_FIELD_MAP_PATH = Path(__file__).parent / "entra_collector.sp_signin.fieldmap.json"


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
    """Shared since-position resolution and Graph pagination for both
    auditLogs endpoints. Accepts a pre-built _GraphClient so both
    sub-collectors share one token and one session."""

    def __init__(
        self,
        *,
        graph: _GraphClient,
        initial_lookback_seconds: Optional[int] = None,
        page_size: int = 100,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._graph = graph
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


class EntraDirectoryAuditCollector(_EntraGraphCollectorBase):
    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        since_dt = self._resolve_since_dt(since_position)
        url = f"{GRAPH_BASE_URL}/auditLogs/directoryAudits"
        params = {
            "$filter": f"activityDateTime ge {since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$top": self._page_size,
            "$orderby": "activityDateTime asc",
        }
        yield from self._graph.get_pages(url, params)


class EntraSignInCollector(_EntraGraphCollectorBase):
    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        since_dt = self._resolve_since_dt(since_position)
        url = f"{GRAPH_BASE_URL}/auditLogs/signIns"
        params = {
            "$filter": f"createdDateTime ge {since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$top": self._page_size,
            "$orderby": "createdDateTime asc",
        }
        yield from self._graph.get_pages(url, params)


class EntraServicePrincipalSignInCollector(_EntraGraphCollectorBase):
    """Polls /auditLogs/servicePrincipalSignIns — OAuth2 client_credentials
    and certificate-authenticated app sign-ins. Requires the same
    AuditLog.Read.All permission as the user sign-in stream."""

    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        since_dt = self._resolve_since_dt(since_position)
        url = f"{GRAPH_BASE_URL}/auditLogs/servicePrincipalSignIns"
        params = {
            "$filter": f"createdDateTime ge {since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$top": self._page_size,
            "$orderby": "createdDateTime asc",
        }
        yield from self._graph.get_pages(url, params)


class EntraCombinedCollector(BaseCollector):
    """Runs all three Entra sub-streams in sequence under a single
    COLLECTORS_DIR entry. Each sub-stream has its own collector_id and
    checkpoint key so they advance independently:
      entra_directory_audits   — account/group/app lifecycle changes
      entra_sign_ins           — interactive and non-interactive user sign-ins
      entra_sp_sign_ins        — service principal sign-ins (daemon/app flows)
    """

    def __init__(
        self,
        *,
        directory_audits: EntraDirectoryAuditCollector,
        sign_ins: EntraSignInCollector,
        sp_sign_ins: EntraServicePrincipalSignInCollector,
        **base_kwargs: Any,
    ):
        super().__init__(**base_kwargs)
        self._directory_audits = directory_audits
        self._sign_ins = sign_ins
        self._sp_sign_ins = sp_sign_ins

    # poll/next_position/map_to_event are not used — run() delegates entirely
    # to sub-collectors which each have their own complete implementations.
    def poll(self, since_position: Optional[str]) -> Iterator[RawActivity]:
        raise NotImplementedError

    def next_position(self, activity: RawActivity) -> str:
        raise NotImplementedError

    def map_to_event(self, activity: RawActivity, actor_global_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def run(self) -> Iterator[dict[str, Any]]:
        yield from self._directory_audits.run()
        yield from self._sign_ins.run()
        yield from self._sp_sign_ins.run()


# ---------------------------------------------------------------------------
# Discovery entry point — returns the combined collector.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    import json

    tenant_id = config["entra_tenant_id"]
    client_id = config["entra_client_id"]
    client_secret = config["entra_client_secret"]
    initial_lookback_seconds = config.get("entra_initial_lookback_seconds", 3600)
    checkpoint_store = CheckpointStore(Path(config["checkpoint_path"]))
    correlator = PassthroughCorrelator()

    # Both sub-collectors share the same Graph client (one token, one session)
    # and the same CheckpointStore, but each has its own collector_id so their
    # checkpoint positions are tracked independently in the store.
    graph = _GraphClient(tenant_id, client_id, client_secret)

    directory_audits = EntraDirectoryAuditCollector(
        graph=graph,
        initial_lookback_seconds=initial_lookback_seconds,
        field_map=json.loads(FIELD_MAP_PATH.read_text()),
        source_timezone=timezone.utc,
        collector_id="entra_directory_audits",
        source_system="entra_id",
        correlator=correlator,
        checkpoint_store=checkpoint_store,
    )

    sign_ins = EntraSignInCollector(
        graph=graph,
        initial_lookback_seconds=initial_lookback_seconds,
        field_map=json.loads(SIGNIN_FIELD_MAP_PATH.read_text()),
        source_timezone=timezone.utc,
        collector_id="entra_sign_ins",
        source_system="entra_id",
        correlator=correlator,
        checkpoint_store=checkpoint_store,
    )

    sp_sign_ins = EntraServicePrincipalSignInCollector(
        graph=graph,
        initial_lookback_seconds=initial_lookback_seconds,
        field_map=json.loads(SP_SIGNIN_FIELD_MAP_PATH.read_text()),
        source_timezone=timezone.utc,
        collector_id="entra_sp_sign_ins",
        source_system="entra_id",
        correlator=correlator,
        checkpoint_store=checkpoint_store,
    )

    return EntraCombinedCollector(
        directory_audits=directory_audits,
        sign_ins=sign_ins,
        sp_sign_ins=sp_sign_ins,
        collector_id="entra_collector",
        source_system="entra_id",
        correlator=correlator,
        checkpoint_store=checkpoint_store,
    )
