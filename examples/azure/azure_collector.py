# examples/azure_collector.py
"""
Azure Activity Log Collector — Azure Resource Manager's Activity Log REST
API (distinct from Entra ID's Graph API). Answers "what happened to this
subscription's resources," not "what happened to a directory account."

Endpoint:
    GET https://management.azure.com/subscriptions/{subscriptionId}
        /providers/Microsoft.Insights/eventtypes/management/values
        ?api-version=2015-04-01
        &$filter=eventTimestamp ge '{start}' and eventTimestamp le '{end}'

Auth: OAuth2 client_credentials against Microsoft's identity platform,
scope https://management.azure.com/.default — same TokenClient reuse
pattern as Entra, but a SEPARATE token/scope/app-registration concern.

Real API constraint: $filter doesn't support operationName server-side.
Every event in the time range comes back over the wire; scoping to
relevant operations happens client-side, in poll_records() (an inclusion
decision, same category as AWS's event_names filter — stays Python, not
part of the field map).

operation_names defaults to the two RBAC role-assignment operations, the
actual access-governance signal in this log:
    Microsoft.Authorization/roleAssignments/write   (grant)
    Microsoft.Authorization/roleAssignments/delete  (revoke)
Pass operation_names=None to disable filtering entirely.

Field mapping is declarative — see azure_collector.fieldmap.json. This
module's own job is now purely ARM API mechanics: OAuth2 token handling,
nextLink pagination, and the operation_names inclusion filter.
poll_records() yields raw ARM API items unmodified (aside from the
operation_names filter).

Azure's status.value uses its own vocabulary ("Succeeded"/"Failed"/
"Started"), not the "Success"/"Failure" strings elsewhere in this
project — handled by the "azure_status_to_outcome" field map transform.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector
from iga_collectors.uploader import TokenClient

ARM_BASE_URL = "https://management.azure.com"
ACTIVITY_LOG_API_VERSION = "2015-04-01"

DEFAULT_OPERATION_NAMES = frozenset({
    # RBAC role assignments (grants and revokes)
    "Microsoft.Authorization/roleAssignments/write",
    "Microsoft.Authorization/roleAssignments/delete",
    # Custom role definitions
    "Microsoft.Authorization/roleDefinitions/write",
    "Microsoft.Authorization/roleDefinitions/delete",
    # Classic administrator role membership
    "Microsoft.Authorization/classicAdministrators/write",
    "Microsoft.Authorization/classicAdministrators/delete",
    # Azure Policy assignments
    "Microsoft.Authorization/policyAssignments/write",
    "Microsoft.Authorization/policyAssignments/delete",
    # Managed identity lifecycle
    "Microsoft.ManagedIdentity/userAssignedIdentities/write",
    "Microsoft.ManagedIdentity/userAssignedIdentities/delete",
    # Managed identity federation credentials (workload identity federation)
    "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/write",
    "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/delete",
})

FIELD_MAP_PATH = Path(__file__).parent / "azure_collector.fieldmap.json"


class AzureActivityLogCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        subscription_id: str,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        operation_names: Optional[frozenset[str]] = DEFAULT_OPERATION_NAMES,
        initial_lookback_seconds: Optional[int] = None,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._subscription_id = subscription_id
        self._operation_names = operation_names
        self._initial_lookback_seconds = initial_lookback_seconds
        self._token_client = TokenClient(
            token_url=f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            client_id=client_id,
            client_secret=client_secret,
            scope="https://management.azure.com/.default",
            session=session,
            timeout=timeout,
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

        filter_str = (
            f"eventTimestamp ge '{since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
            f"and eventTimestamp le '{now.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
        )
        url = (
            f"{ARM_BASE_URL}/subscriptions/{self._subscription_id}"
            f"/providers/Microsoft.Insights/eventtypes/management/values"
        )
        params = {"api-version": ACTIVITY_LOG_API_VERSION, "$filter": filter_str}

        while url:
            headers = {"Authorization": f"Bearer {self._token_client.get_token()}"}
            response = self._session.get(url, headers=headers, params=params, timeout=self._timeout)
            response.raise_for_status()
            body = response.json()

            for item in body.get("value", []):
                operation_name = (item.get("operationName") or {}).get("value")
                if not operation_name:
                    continue
                if self._operation_names is not None and operation_name not in self._operation_names:
                    continue
                yield item

            url = body.get("nextLink")
            params = None  # nextLink already carries the query string


# ---------------------------------------------------------------------------
# Reference example: RBAC role-assignment grants/revokes only.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    import json
    operation_names = config.get("azure_operation_names")
    operation_names = frozenset(operation_names) if operation_names else DEFAULT_OPERATION_NAMES
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return AzureActivityLogCollector(
        subscription_id=config["azure_subscription_id"],
        tenant_id=config["azure_tenant_id"],
        client_id=config["azure_client_id"],
        client_secret=config["azure_client_secret"],
        operation_names=operation_names,
        initial_lookback_seconds=config.get("azure_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="azure_activity_log",
        source_system="azure_activity_log",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )
