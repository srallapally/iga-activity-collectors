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

application_names controls which Workspace applications to poll. Multiple
applications are fetched in a single collector run (login, admin, token).
Each application can have its own event_name->result mapping entry in
event_name_to_result. Pass application_names=["login"] to restore the
previous single-app behaviour.

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

DEFAULT_APPLICATION_NAMES = ["login", "admin", "token"]

DEFAULT_EVENT_NAME_TO_RESULT = {
    # login application
    "login_success": "success",
    "login_failure": "failure",
    "logout": "success",
    # admin application — account/group lifecycle
    "CREATE_USER": "success",
    "DELETE_USER": "success",
    "SUSPEND_USER": "success",
    "UNSUSPEND_USER": "success",
    "RENAME_USER": "success",
    "CREATE_GROUP": "success",
    "DELETE_GROUP": "success",
    "ADD_GROUP_MEMBER": "success",
    "REMOVE_GROUP_MEMBER": "success",
    # token application — OAuth2 grants and revocations
    "AUTHORIZE": "success",
    "REVOKE": "success",
}

FIELD_MAP_PATH = Path(__file__).parent / "google_workspace_collector.fieldmap.json"


class GoogleWorkspaceReportsCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        service_account_key_path: str,
        admin_email: str,
        application_names: Optional[list] = None,
        event_name_to_result: Optional[dict[str, str]] = None,
        initial_lookback_seconds: Optional[int] = None,
        max_results: int = 1000,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        token_provider: Optional[Callable[[], str]] = None,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._application_names = application_names if application_names is not None else DEFAULT_APPLICATION_NAMES
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

        for application_name in self._application_names:
            yield from self._poll_application(since_dt, now, application_name)

    def _poll_application(
        self, since_dt: datetime, now: datetime, application_name: str
    ) -> Iterator[dict[str, Any]]:
        url = f"{REPORTS_API_BASE}/all/applications/{application_name}"
        params: Optional[dict] = {
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
                yield from self._activity_to_records(item, application_name)

            next_token = body.get("nextPageToken")
            if next_token:
                params = {**params, "pageToken": next_token}
            else:
                url = None

    def _activity_to_records(
        self, item: dict[str, Any], application_name: str = ""
    ) -> Iterator[dict[str, Any]]:
        actor = item.get("actor") or {}
        # profileId is the immutable Google account identifier; email is mutable.
        actor_id = actor.get("profileId") or actor.get("email")
        if not actor_id:
            return

        item_id = item.get("id") or {}
        event_time = item_id.get("time")
        if not event_time:
            return

        app_name = item_id.get("applicationName") or application_name
        for event in item.get("events", []):
            event_name = event.get("name")
            if not event_name:
                continue
            yield {
                "actor_id": actor_id,
                "action": event_name,
                "applicationName": app_name,
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
        application_names=config.get("gws_application_names", DEFAULT_APPLICATION_NAMES),
        initial_lookback_seconds=config.get("gws_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="google_workspace_login",
        source_system="google_workspace",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )
