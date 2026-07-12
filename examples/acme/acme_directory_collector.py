# COLLECTORS_DIR/acme_directory_collector.py
"""
Tutorial example: a fictional SaaS identity provider, "Acme Directory",
with a simple REST API:

    GET https://api.acmedirectory.example.com/v1/events
        ?since=<ISO8601>&limit=100&cursor=<opaque>
        Authorization: Bearer <api_key>

    {
      "events": [
        {
          "id": "evt_123",
          "type": "user.login",
          "occurred_at": "2026-07-10T10:44:47Z",
          "actor": {"email": "alice@acme.com"},
          "target": {"name": "AcmeApp"},
          "success": true
        }
      ],
      "next_cursor": "abc123"  // or null when no more pages
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

FIELD_MAP_PATH = Path(__file__).parent / "acme_directory_collector.fieldmap.json"


class AcmeDirectoryCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        initial_lookback_seconds: Optional[int] = None,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._api_base_url = api_base_url.rstrip("/")
        self._api_key = api_key
        self._initial_lookback_seconds = initial_lookback_seconds
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

        headers = {"Authorization": f"Bearer {self._api_key}"}
        params: Optional[dict[str, Any]] = {
            "since": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 100,
        }
        url = f"{self._api_base_url}/v1/events"

        while True:
            response = self._session.get(url, headers=headers, params=params, timeout=self._timeout)
            response.raise_for_status()
            body = response.json()

            yield from body.get("events", [])

            next_cursor = body.get("next_cursor")
            if not next_cursor:
                break
            params = {"since": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 100, "cursor": next_cursor}


def create_collector(config: dict[str, Any]):
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return AcmeDirectoryCollector(
        api_base_url=config["acme_api_base_url"],
        api_key=config["acme_api_key"],
        initial_lookback_seconds=config.get("acme_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="acme_directory",
        source_system="acme_directory",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )