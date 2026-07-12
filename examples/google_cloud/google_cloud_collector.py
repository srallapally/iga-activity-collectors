# examples/google_cloud_collector.py
"""
Google Cloud Collector — Cloud Audit Logs via the Cloud Logging API v2
entries.list method (POST to https://logging.googleapis.com/v2/entries:list
with a JSON body).

Auth is simpler than google_workspace_collector.py's: GCP service accounts
don't need domain-wide delegation or user impersonation. Still uses
google-auth directly, with the same injectable token_provider testability
pattern.

Log name encoding: the `/` in `cloudaudit.googleapis.com/activity` is
percent-encoded as `%2F` inside the filter string itself — Cloud
Logging's own filter syntax convention.

method_names filtering is an inclusion decision and stays Python, in
poll_records() — same category as every other cloud collector's
operation/event-name filter here.

Field mapping is declarative — see google_cloud_collector.fieldmap.json.
Outcome derivation (protoPayload.status.code absent/0 = success, else
failure) is simple enough to be a pure transform ("gcp_status_code_to_outcome")
rather than needing Python — unlike Salesforce's LOGIN_STATUS comparison,
this doesn't depend on any per-instance configuration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import requests

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

LOGGING_API_URL = "https://logging.googleapis.com/v2/entries:list"
LOGGING_SCOPE = "https://www.googleapis.com/auth/logging.read"

DEFAULT_METHOD_NAMES = frozenset({
    "SetIamPolicy",
    "google.iam.admin.v1.CreateServiceAccount",
    "google.iam.admin.v1.DeleteServiceAccount",
    "google.iam.admin.v1.CreateServiceAccountKey",
    "google.iam.admin.v1.DeleteServiceAccountKey",
})

FIELD_MAP_PATH = Path(__file__).parent / "google_cloud_collector.fieldmap.json"


class GoogleCloudAuditLogCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        project_id: str,
        service_account_key_path: str,
        method_names: Optional[frozenset[str]] = DEFAULT_METHOD_NAMES,
        log_type: str = "activity",
        initial_lookback_seconds: Optional[int] = None,
        page_size: int = 100,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        token_provider: Optional[Callable[[], str]] = None,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._project_id = project_id
        self._method_names = method_names
        self._log_type = log_type
        self._initial_lookback_seconds = initial_lookback_seconds
        self._page_size = page_size
        self._session = session or requests.Session()
        self._timeout = timeout
        self._token_provider = token_provider or _make_default_token_provider(service_account_key_path)

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

        log_name = f"projects/{self._project_id}/logs/cloudaudit.googleapis.com%2F{self._log_type}"
        filter_str = (
            f'logName="{log_name}" AND '
            f'timestamp>="{since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
        )
        body: dict[str, Any] = {
            "resourceNames": [f"projects/{self._project_id}"],
            "filter": filter_str,
            "orderBy": "timestamp asc",
            "pageSize": self._page_size,
        }

        while True:
            headers = {"Authorization": f"Bearer {self._token_provider()}"}
            response = self._session.post(
                LOGGING_API_URL, headers=headers, json=body, timeout=self._timeout
            )
            response.raise_for_status()
            result = response.json()

            for entry in result.get("entries", []):
                method_name = (entry.get("protoPayload") or {}).get("methodName")
                if not method_name:
                    continue
                if self._method_names is not None and method_name not in self._method_names:
                    continue
                yield entry

            next_page_token = result.get("nextPageToken")
            if not next_page_token:
                break
            body["pageToken"] = next_page_token


def _make_default_token_provider(key_path: str) -> Callable[[], str]:
    state: dict[str, Any] = {"credentials": None}

    def provider() -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if state["credentials"] is None:
            state["credentials"] = service_account.Credentials.from_service_account_file(
                key_path, scopes=[LOGGING_SCOPE],
            )
        credentials = state["credentials"]
        if not credentials.valid:
            credentials.refresh(Request())
        return credentials.token

    return provider


# ---------------------------------------------------------------------------
# Reference example.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    import json
    method_names = config.get("gcp_method_names")
    method_names = frozenset(method_names) if method_names else DEFAULT_METHOD_NAMES
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return GoogleCloudAuditLogCollector(
        project_id=config["gcp_project_id"],
        service_account_key_path=config["gcp_service_account_key_path"],
        method_names=method_names,
        initial_lookback_seconds=config.get("gcp_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="gcp_audit_iam",
        source_system="google_cloud",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )
