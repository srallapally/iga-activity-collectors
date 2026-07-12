# examples/google_workspace_collector.py
"""
Google Workspace Collector — Admin SDK Reports API.

Auth: SERVICE ACCOUNT with DOMAIN-WIDE DELEGATION, JWT-bearer assertion
(RFC 7523) — genuinely different from every Microsoft collector's OAuth2
client_credentials; TokenClient does not apply. Uses `google-auth`
directly (not the full google-api-python-client). Token acquisition is
behind an injectable `token_provider` callable (same testability pattern
as JDBC's connect_fn, Kafka's consumer_factory) so tests never need
google-auth installed.

DESIGN BOUNDARY, same reasoning as windows_eventlog_collector.py's
EVENT_ID_ACTIONS: event_name_to_result is a LOOKUP TABLE (event name ->
result), not a path into a record, so it stays in Python
(poll_records/_activity_to_records), which pre-attaches the looked-up
result to each yielded record under _resolved_result; the field map then
does plain path resolution against that.

Structural reshaping ALSO stays in poll_records(), same category as
OTel's resourceSpans/scopeSpans/spans flattening: each raw "activity" the
API returns can bundle MULTIPLE events in its `events` array — this is
flattened to one flat record per inner event before the field map ever
sees it.

Field mapping is declarative — see google_workspace_collector.fieldmap.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import requests

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

REPORTS_API_SCOPE = "https://www.googleapis.com/auth/admin.reports.audit.readonly"
REPORTS_API_BASE = "https://admin.googleapis.com/admin/reports/v1/activity/users"

DEFAULT_EVENT_NAME_TO_RESULT = {
    "login_success": "Success",
    "login_failure": "Failure",
}

FIELD_MAP_PATH = Path(__file__).parent / "google_workspace_collector.fieldmap.json"


class GoogleWorkspaceReportsCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        service_account_key_path: str,
        admin_email: str,
        application_name: str = "login",
        event_name_to_result: Optional[dict[str, str]] = None,
        initial_lookback_seconds: Optional[int] = None,
        max_results: int = 1000,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        token_provider: Optional[Callable[[], str]] = None,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._application_name = application_name
        self._event_name_to_result = event_name_to_result or DEFAULT_EVENT_NAME_TO_RESULT
        self._initial_lookback_seconds = initial_lookback_seconds
        self._max_results = max_results
        self._session = session or requests.Session()
        self._timeout = timeout
        self._token_provider = token_provider or _make_default_token_provider(
            service_account_key_path, admin_email
        )

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

        url = f"{REPORTS_API_BASE}/all/applications/{self._application_name}"
        params = {
            "startTime": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxResults": self._max_results,
        }

        while url:
            headers = {"Authorization": f"Bearer {self._token_provider()}"}
            response = self._session.get(url, headers=headers, params=params, timeout=self._timeout)
            response.raise_for_status()
            body = response.json()

            for item in body.get("items", []):
                yield from self._activity_to_records(item)

            next_token = body.get("nextPageToken")
            if next_token:
                params = {**params, "pageToken": next_token}
            else:
                url = None

    def _activity_to_records(self, item: dict[str, Any]) -> Iterator[dict[str, Any]]:
        actor = item.get("actor") or {}
        user = actor.get("email")
        if not user:
            return

        item_id = item.get("id") or {}
        event_time = item_id.get("time")
        if not event_time:
            return

        for event in item.get("events", []):
            event_name = event.get("name")
            if not event_name:
                continue
            yield {
                "user": user,
                "action": event_name,
                "applicationName": item_id.get("applicationName") or self._application_name,
                "time": event_time,
                "_resolved_result": self._event_name_to_result.get(event_name, ""),
            }


def _make_default_token_provider(key_path: str, admin_email: str) -> Callable[[], str]:
    state: dict[str, Any] = {"credentials": None}

    def provider() -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if state["credentials"] is None:
            state["credentials"] = service_account.Credentials.from_service_account_file(
                key_path, scopes=[REPORTS_API_SCOPE], subject=admin_email,
            )
        credentials = state["credentials"]
        if not credentials.valid:
            credentials.refresh(Request())
        return credentials.token

    return provider


# ---------------------------------------------------------------------------
# Reference example: sign-in activity.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return GoogleWorkspaceReportsCollector(
        service_account_key_path=config["gws_service_account_key_path"],
        admin_email=config["gws_admin_email"],
        application_name=config.get("gws_application_name", "login"),
        initial_lookback_seconds=config.get("gws_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="google_workspace_login",
        source_system="google_workspace",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )
