# examples/okta_collector.py
"""
Okta Collector — System Log API (GET https://{org}.okta.com/api/v1/logs).

Auth: static API token via `Authorization: SSWS {token}` — no token
exchange, a genuine simplification versus every OAuth2/JWT-bearer
collector elsewhere in this project. OAuth2 for Okta (private-key-signed
JWT client assertion) is a real alternative, not implemented here.

Pagination: standard RFC 5988 `Link` response header, followed until
absent.

event_types filtering is an inclusion decision and stays Python, in
poll_records() — same category as every other cloud collector's
operation/event-name filter here. Everything else about this collector's
mapping is pure declarative path resolution — no lookup tables, no
per-instance-parameterized comparisons, so poll_records() needs almost no
pre-computation: it yields raw System Log items essentially unmodified.

Field mapping is declarative — see okta_collector.fieldmap.json. Notably
straightforward compared to the other cloud collectors: target[0].alternateId
with a fallback to target[0].displayName is expressible directly as a
source_path fallback list with array indexing, and outcome.result passes
straight through the shared sailpoint_result_to_outcome transform (Okta's
SUCCESS/FAILURE vocabulary already matches once lowercased).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

DEFAULT_EVENT_TYPES = frozenset({
    "user.session.start",
    "user.lifecycle.create", "user.lifecycle.deactivate",
    "user.lifecycle.suspend", "user.lifecycle.unsuspend",
    "user.account.lock", "user.account.privilege.grant", "user.account.privilege.revoke",
    "group.user_membership.add", "group.user_membership.remove",
})

FIELD_MAP_PATH = Path(__file__).parent / "okta_collector.fieldmap.json"


def _next_link(response: requests.Response) -> Optional[str]:
    return response.links.get("next", {}).get("url")


class OktaSystemLogCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        org_url: str,
        api_token: str,
        event_types: Optional[frozenset[str]] = DEFAULT_EVENT_TYPES,
        initial_lookback_seconds: Optional[int] = None,
        limit: int = 100,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._org_url = org_url.rstrip("/")
        self._api_token = api_token
        self._event_types = event_types
        self._initial_lookback_seconds = initial_lookback_seconds
        self._limit = limit
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

        url = f"{self._org_url}/api/v1/logs"
        params: Optional[dict[str, Any]] = {
            "since": since_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "limit": self._limit,
            "sortOrder": "ASCENDING",
        }
        headers = {"Authorization": f"SSWS {self._api_token}", "Accept": "application/json"}

        while url:
            response = self._session.get(url, headers=headers, params=params, timeout=self._timeout)
            response.raise_for_status()

            for item in response.json():
                event_type = item.get("eventType")
                if not event_type:
                    continue
                if self._event_types is not None and event_type not in self._event_types:
                    continue
                yield item

            url = _next_link(response)
            params = None


# ---------------------------------------------------------------------------
# Reference example.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    import json
    event_types = config.get("okta_event_types")
    event_types = frozenset(event_types) if event_types else DEFAULT_EVENT_TYPES
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return OktaSystemLogCollector(
        org_url=config["okta_org_url"],
        api_token=config["okta_api_token"],
        event_types=event_types,
        initial_lookback_seconds=config.get("okta_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="okta_system_log",
        source_system="okta",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )