# Writing a new collector

This walks through building a collector from nothing, using a fictional
source ("Acme Directory," a made-up SaaS identity provider) as the
running example. Every code block below was actually run against the
real framework before being written down here — none of it is
pseudocode.

## What a collector's job actually is

A collector turns activity from some source into records matching
`activity_log_schema_v4.json`, then hands them off for upload. The
framework does the parts that are the same for every collector:
correlating a raw actor ID to an IGA identity, tracking checkpoints so
you don't re-upload the same events twice, batching and uploading to the
IGA API. **Your job is two things, and they're separable:**

1. **API mechanics** — how to authenticate, how to page through results,
   which raw records are even worth including.
2. **Field mapping** — deciding that a raw field like `type` means the
   canonical `action`, or that `actor.email` should become the actor
   identity.

Keeping these separate is the actual design principle behind everything
below. (1) stays Python, because "call this endpoint, follow this
cursor" isn't reasonably expressible any other way. (2) is JSON — a
declarative field map that a shared interpreter applies, not code you
write per collector.

## Choosing a base class

There are three, and you almost always want the third:

- **`BaseCollector`** — the raw abstract base. Implement `poll()`,
  `next_position()`, `map_to_event()` yourself. Use this only if your
  source genuinely doesn't fit "a stream of record-shaped raw items,"
  which is rare.
- **`TabularActivityCollector`** — the older, Python-code-driven model
  (a `ColumnMap` plus a `_record_to_values` method). Still used by
  several collectors in this project that predate the declarative model.
  Don't use this for new collectors; it's here for context, not as a
  recommendation.
- **`DeclarativeMappedCollector`** (in `iga_collectors.field_mapping`)
  — **use this.** You implement one method, `poll_records()`, that
  yields raw dicts. A field map JSON document, loaded and interpreted
  generically, does everything from there.

## The three files

Every collector in `COLLECTORS_DIR` is three files, same stem:

```
acme_directory_collector.py            # code: auth, pagination, inclusion filtering
acme_directory_collector.fieldmap.json # declarative: raw field -> canonical field
acme_directory_collector.json          # customer's own credentials/settings
```

`discovery.py` finds the `.py` file and merges its sibling `.json` (the
credentials one) with the shared base config before calling
`create_collector(config)`. The `.fieldmap.json` is different — your own
`create_collector()` loads it directly via a relative path; discovery
never touches it.

## Step 1: know your raw data

Acme Directory's fictional API:

```
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
  "next_cursor": "abc123"   // or null when there are no more pages
}
```

Static bearer token auth, cursor pagination, one nesting level on
`actor`/`target`, and a plain boolean for success/failure. Deliberately
simple — real sources usually have one or two more wrinkles (see "Real
wrinkles you'll hit" below), but this covers the core shape.

## Step 2: write `poll_records()` — API mechanics only

```python
# acme_directory_collector.py
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
```

Notice what's **not** in here: no field extraction, no outcome logic, no
timestamp parsing. `poll_records()` yields the raw event dicts exactly as
the API returns them. That's the whole job of this file.

**Checkpoint filtering is handled for you.** `DeclarativeMappedCollector.poll()`
compares each record's resolved timestamp against the last checkpoint and
drops anything at or before it — you don't need to (and shouldn't)
duplicate that logic here, even though `since_dt` is also used above to
build the `since` query parameter. Those are two different things: the
query parameter asks the server to filter server-side; the checkpoint
filter is a client-side safety net in case the server's boundary
semantics don't exactly match yours. Older, `TabularActivityCollector`-
based collectors in this project had to implement that filter manually
in every single one; the declarative model does it once, centrally.

## Step 3: write the field map — declares the mapping, does nothing else

```json
{
  "source_system": "acme_directory",
  "event_type": "authentication",
  "fields": {
    "_native_actor_id": {
      "source_path": "actor.email",
      "required": true
    },
    "_event_time": {
      "source_path": "occurred_at",
      "transform": "parse_iso8601",
      "required": true
    },
    "action": {
      "source_path": "type",
      "required": true
    },
    "outcome": {
      "source_path": "success",
      "transform": "bool_to_outcome",
      "default": "unknown"
    },
    "resource.resource_name": {
      "source_path": "target.name",
      "default": "unknown_resource"
    }
  }
}
```

Reference:

| Key | Meaning |
|---|---|
| `source_path` | Dotted path into the raw record. `"actor.email"` walks `record["actor"]["email"]`. Array indexing: `"a[0].b"`. Can be a list of paths tried in order (first non-`None` wins) — for "prefer this field, fall back to that one" patterns. |
| `literal` | A fixed value, no record lookup. Mutually exclusive with `source_path`. |
| `transform` | Name of a function from the shared registry (see below), applied to the resolved value. |
| `default` | Used if the resolved (and transformed) value is `None`. |
| `required` | If `true` and the final value is still `None`, the **whole record is dropped**, not just that field. |

Two field names are special and don't get written into the event
literally: `_native_actor_id` (fed to your `IdentityCorrelator`) and
`_event_time` (must resolve to a `datetime`; drives both the event
timestamp and the checkpoint). Everything else is a real dotted path into
`activity_log_schema_v4.json` — `"resource.resource_name"`,
`"environment.source_ip"`, `"auth_context.client_id"`, whatever the
schema actually has.

### `$json`: when a field is itself a JSON-encoded string

Some APIs (CloudTrail is the example already in this project) embed a
whole sub-object as a serialized string inside one field. A `$json`
path segment means "parse the current value as JSON, then keep going":

```json
"environment.source_ip": {
  "source_path": "CloudTrailEvent.$json.sourceIPAddress"
}
```

### Available transforms

From `iga_collectors.field_mapping.FIELD_TRANSFORMS`:

| Name | Does |
|---|---|
| `identity` | Passthrough (the default if `transform` is omitted). |
| `parse_iso8601` | Handles a trailing `Z` and truncates fractional seconds beyond 6 digits — several real APIs return 7. |
| `sailpoint_result_to_outcome` | Maps `"Success"`/`"Failure"` (case-insensitive) to canonical `"success"`/`"failure"`. |
| `bool_to_outcome` | Maps a plain boolean to `"success"`/`"failure"` — added for this tutorial's example; a real source with a boolean success flag would need exactly this. |
| `error_code_presence_to_outcome` | For sources whose only failure signal is an `errorCode`-shaped nested object. |
| `access_key_to_credential_type` | Maps presence of an access key ID to the `credential_type` enum value `api_key`. |

**Adding a new one is a two-line change** in `field_mapping.py`: write a
function taking the resolved value and returning the canonical value (or
`None`), register it in `FIELD_TRANSFORMS` under a name. That's genuinely
how `bool_to_outcome` got added for this tutorial — it didn't exist until
this example needed it.

## Step 4: the connection config

```json
{
  "acme_api_base_url": "https://api.acmedirectory.example.com",
  "acme_api_key": "REPLACE_ME",
  "acme_initial_lookback_seconds": 3600
}
```

Just whatever `config[...]` keys your `create_collector()` reads.
`checkpoint_path` is supplied automatically by the shared base config —
don't put it here.

## Step 5: test without a real API

Inject a fake `requests.Session` — this is the same pattern every
collector in this project uses, and it's how the code above was actually
verified before being written into this tutorial:

```python
class FakeResponse:
    def __init__(self, body):
        self._body = body
    def raise_for_status(self): pass
    def json(self): return self._body

class FakeSession:
    def __init__(self, pages):
        self._pages = pages
    def get(self, url, headers=None, params=None, timeout=None):
        return FakeResponse(self._pages.pop(0))

collector = create_collector({
    "acme_api_base_url": "https://api.acmedirectory.example.com",
    "acme_api_key": "testkey",
    "checkpoint_path": "/tmp/checkpoints.json",
})
collector._session = FakeSession([
    {"events": [{"id": "evt_1", "type": "user.login",
                 "occurred_at": "2026-07-10T10:44:47Z",
                 "actor": {"email": "alice@acme.com"},
                 "target": {"name": "AcmeApp"}, "success": True}],
     "next_cursor": None},
])

events = list(collector.run())
assert events[0]["action"] == "user.login"
assert events[0]["outcome"] == "success"
assert events[0]["actor_global_id"] == "alice@acme.com"
```

No network, no real credentials, no `boto3`/`google-auth`/whatever your
real source needs installed just to run this test.

## Common pitfalls (all real, all caught while building this project)

- **`event_type` goes in the field map JSON, not the constructor.**
  `DeclarativeMappedCollector` reads `event_type` from the field map
  document. Passing `event_type=` as a constructor keyword argument
  raises `TypeError: BaseCollector.__init__() got an unexpected keyword
  argument 'event_type'` — this is a real mistake made and fixed while
  building this very tutorial.
- **Naive datetimes get rejected, not silently misinterpreted.** If
  `_event_time` resolves to a timezone-naive `datetime`, the framework
  attaches `source_timezone` rather than guessing. Always pass an
  explicit `source_timezone` and know what timezone your source's
  timestamps are actually in.
- **"No evidence" is not the same as "success."** If a field might be
  missing entirely (no result data at all), don't let a transform
  quietly return `"success"` for that case. Return `None` and let
  `"default"` in the field map make the deliberate choice (usually
  `"unknown"`). This exact bug — an outcome transform asserting success
  when it should have said "we don't know" — was caught during self-
  review while extending the AWS collector, twice.
- **A `source_path` that resolves to `None` isn't an error** — it's the
  normal case for "this field wasn't in this particular record." Use
  `default` or `required` to say what should happen; don't assume every
  field is always present.
- **Test your field map, not just your Python.** Most of the actual
  logic in a `DeclarativeMappedCollector`-based collector lives in the
  JSON now. A wrong `source_path` typo produces `None` silently (falling
  through to `default` or dropping the record if `required`), not an
  exception — write a real test with realistic sample data, don't just
  eyeball the JSON.

## Real wrinkles you'll hit that this tutorial's example doesn't show

Acme Directory was kept simple on purpose. Real sources in this project
needed more:

- **Multi-step API flows** (Office 365's subscribe → list content →
  fetch blob; Salesforce's query → fetch CSV blob) — still just Python
  in `poll_records()`, but genuinely more steps.
- **Lookup-table-driven inclusion/enrichment** (Windows EventLog's event
  ID → action/result table) — stays in Python, because it's not a path
  into a record, it's a table keyed by a field's value. `poll_records()`
  pre-computes the looked-up fields and attaches them to the record
  under a `_resolved_*`-prefixed key; the field map then does plain path
  resolution against those.
- **Structural reshaping before mapping makes sense** (OTel/Jaeger's
  nested attribute-list-to-dict flattening) — also Python, in
  `poll_records()`, before yielding.
- **Genuinely different auth models** — OAuth2 client_credentials
  (`iga_collectors.uploader.TokenClient`, reusable as-is against any
  token endpoint), JWT-bearer with service-account impersonation
  (`google-auth`), a plain static API token, AWS's own credential chain.
  Pick whichever matches your actual source; don't force one collector's
  auth pattern onto a source that doesn't use it.

None of these change Step 3 — the field map still only describes "raw
field means canonical field." They change how much Python goes into
Step 2.