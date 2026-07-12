# Developer Cookbook — Writing a Collector with Claude Code

This guide walks you through building a new activity collector for the
`iga-collectors` framework using Claude Code as your development assistant.
By the end you will have three files — a Python collector, a field-map JSON,
and a credentials config — that you can drop into any `COLLECTORS_DIR` and
run immediately.

---

## Prerequisites

- `iga-collectors` SDK installed: `pip install -e ".[dev]"` from the repo root
- Claude Code installed: `npm install -g @anthropic-ai/claude-code` (or the
  desktop app)
- The source system's API documentation open in a browser

---

## Step 1 — Bootstrap with Claude Code

Open a terminal in **your `COLLECTORS_DIR`** (not inside this repo), then
start Claude Code:

```bash
cd /path/to/your/collectors
claude
```

Paste the following prompt, filling in the bracketed sections for your source
system. The prompt is written to give Claude Code exactly the context it needs
— the framework contracts, the file conventions, and a worked example to
imitate.

---

### The bootstrapping prompt

```
I am building an activity collector for the iga-collectors SDK.
The SDK repo is at ~/path/to/iga-activity-collectors (adjust as needed).
Read CLAUDE.md in that repo for the full framework reference before writing
any code.

Source system: <NAME — e.g. "PingOne", "CyberArk Vault", "GitHub">
API documentation: <URL or paste the relevant endpoint docs here>

What the API looks like:
- Base URL: <e.g. https://api.example.com/v1>
- Auth: <e.g. "Bearer token in Authorization header", "OAuth2 client_credentials", "API key in X-Api-Key header">
- Endpoint to poll: <e.g. GET /events?since=ISO8601&limit=100>
- Pagination: <e.g. "nextPageToken field in response body", "Link: rel=next header", "cursor param">
- Timestamp field: <e.g. "createdAt", "timestamp", "eventTime">
- Actor field (immutable ID preferred): <e.g. "actor.id", "userId", "principalId">
- Action field: <e.g. "eventType", "action", "operationName">
- Outcome/result field (if any): <e.g. "outcome.result" SUCCESS/FAILURE, "statusCode", boolean "success">
- Target/resource field (if any): <e.g. "target[0].displayName", "resourceId">

Events I care about (leave blank to capture everything):
<list event type names/codes, one per line, e.g.:
  user.session.start
  user.lifecycle.create
  user.lifecycle.deactivate
>

Please create three files in the current directory under a new subdirectory
called <name>_collector/:

1. <name>_collector.py   — Python collector class
2. <name>_collector.fieldmap.json — declarative field mapping
3. <name>_collector.json — credentials config (with enabled: false and
   REPLACE_ME placeholders)

Follow these rules exactly:
- Subclass DeclarativeMappedCollector (from iga_collectors.field_mapping)
  for REST/cloud sources. Only use TabularActivityCollector for SQL or
  flat-file sources, and BaseCollector directly only if pagination is
  non-standard.
- Implement only poll_records(since_position). Do not override poll(),
  run(), next_position(), or map_to_event().
- Use the injectable session/client pattern for testability (accept an
  optional requests.Session in __init__, default to requests.Session()).
- _native_actor_id must be the most immutable identifier the API provides
  (GUID, numeric ID). Only fall back to email/username if no immutable ID
  exists.
- _event_time must resolve to a datetime via parse_iso8601 or another
  transform from FIELD_TRANSFORMS.
- The collector_id and source_system strings must be snake_case and
  describe the source (e.g. "pingone_audit", "cyberark_vault_audit").
- Use PassthroughCorrelator() in create_collector() — the customer will
  swap in a real correlator for production.
- All credential values in the .json config must be "REPLACE_ME" strings.
- Set "enabled": false in the .json config.

After creating the files, show me how to test the collector with:
  iga-collectors --dry-run --limit 5 --collector <name>_collector
```

---

## Step 2 — Understand what Claude Code produces

Claude Code will create three files. Here is what each one does and what
to check.

### `<name>_collector.py`

The Python file has three parts:

**Constants**
```python
DEFAULT_EVENT_TYPES = frozenset({
    "user.login",
    "user.created",
    ...
})

FIELD_MAP_PATH = Path(__file__).parent / "<name>_collector.fieldmap.json"
```
`DEFAULT_EVENT_TYPES` is the inclusion filter applied in `poll_records()`.
It stays in Python because it is a record-inclusion decision, not a
field-mapping decision. You can override it at runtime via a key in the
`.json` config (the pattern every existing collector uses).

**The collector class**
```python
class MyCollector(DeclarativeMappedCollector):
    def __init__(self, *, api_key, initial_lookback_seconds=None,
                 session=None, **declarative_kwargs):
        super().__init__(**declarative_kwargs)
        self._api_key = api_key
        self._session = session or requests.Session()
        ...

    def poll_records(self, since_position):
        # API mechanics only: auth, pagination, event-type filtering
        ...
        yield record  # raw dict — no field mapping here
```

`poll_records()` is the only method you implement. It must:
- Resolve the start time from `since_position` (ISO timestamp string) or
  fall back to `initial_lookback_seconds`
- Yield raw record dicts exactly as the API returns them
- Follow pagination until exhausted
- Apply the event-type inclusion filter if one exists

**`create_collector(config)`**
```python
def create_collector(config: dict):
    field_map = json.loads(FIELD_MAP_PATH.read_text())
    return MyCollector(
        api_key=config["myapp_api_key"],
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="myapp_audit",
        source_system="myapp",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )
```

This is the discovery entry point. The framework calls it with the merged
config dict (base config + your `.json` file). Always read credentials from
`config`, never hardcode them.

---

### `<name>_collector.fieldmap.json`

```json
{
  "source_system": "myapp",
  "event_type": "resource_access",
  "fields": {
    "_native_actor_id": {
      "source_path": "actor.id",
      "required": true
    },
    "_event_time": {
      "source_path": "createdAt",
      "transform": "parse_iso8601",
      "required": true
    },
    "action": {
      "source_path": "eventType",
      "required": true
    },
    "outcome": {
      "source_path": "result",
      "transform": "sailpoint_result_to_outcome",
      "default": "unknown"
    },
    "resource.resource_name": {
      "source_path": ["target.id", "target.name"],
      "default": "unknown_resource"
    }
  }
}
```

**Reserved fields** (never appear in the output event):

| Field | Purpose |
|---|---|
| `_native_actor_id` | Fed to `IdentityCorrelator`; becomes `actor_global_id` |
| `_event_time` | Drives `event_time` and the checkpoint position |

**All other keys** are dotted paths written directly into the canonical event
dict (`resource.resource_name` → `event["resource"]["resource_name"]`).

**`source_path` rules:**
- String: single path, e.g. `"actor.id"`
- List: first non-null value wins, e.g. `["actor.id", "actor.email"]`
- `[N]` indexing: `"targets[0].displayName"`
- `.$json`: parse the current value as a JSON string and continue,
  e.g. `"details.$json.errorCode"`

**Available transforms:**

| Transform | When to use |
|---|---|
| `parse_iso8601` | String timestamp → datetime (handles `Z`, 7-digit fractional seconds) |
| `bool_to_outcome` | `true`/`false` → `"success"`/`"failure"` |
| `sailpoint_result_to_outcome` | `"Success"`/`"Failure"` strings (case-insensitive) |
| `error_code_presence_to_outcome` | Dict with optional `errorCode` field → outcome |
| `graph_signin_error_code_to_outcome` | Integer `errorCode`, 0 = success |
| `azure_status_to_outcome` | `"Succeeded"`/`"Failed"` strings |
| `gcp_status_code_to_outcome` | Integer, 0 = success |
| `access_key_to_credential_type` | Non-null key ID → `"api_key"` |
| `parse_salesforce_timestamp` | `"YYYYMMDDHHMMSS.mmm"` format |
| `unix_nanos_to_datetime` | Integer nanoseconds since epoch |
| `micros_to_datetime` | Integer microseconds since epoch |
| `otel_status_code_to_outcome` | OTLP status code int or string |
| `identity` | No-op passthrough |

If none of these fit, add a new transform to
`src/iga_collectors/field_mapping.py` and register it in `FIELD_TRANSFORMS`.

---

### `<name>_collector.json`

```json
{
  "enabled": false,
  "myapp_api_key": "REPLACE_ME",
  "myapp_initial_lookback_seconds": 3600
}
```

- `"enabled": false` — collector is skipped until you set it to `true`
- All credential keys are prefixed with the source name to avoid collisions
  when multiple collectors share the same `COLLECTORS_DIR`
- `checkpoint_path` is always injected by the framework — do not add it here

---

## Step 3 — Test without IGA credentials

```bash
# Dry run: poll the API, map events, print to stdout — no upload
COLLECTORS_DIR=/path/to/your/collectors \
  iga-collectors --dry-run --limit 5 --collector <name>_collector

# See raw API records and mapped event dicts
COLLECTORS_DIR=/path/to/your/collectors \
  iga-collectors --dry-run --limit 5 --collector <name>_collector \
  --log-level DEBUG 2>&1 | head -100
```

Set `"enabled": true` in the `.json` config first.

**What to look for:**

| Check | Sign it works | Sign it doesn't |
|---|---|---|
| Records reach the fieldmap | `DEBUG raw record collector=...` lines | No raw record lines — `poll_records()` yielded nothing |
| `_native_actor_id` resolves | `actor_global_id` in output is not `"system"` or empty | `DEBUG record dropped required field missing ... _native_actor_id` |
| `_event_time` resolves | `event_time` in output is a valid ISO timestamp | `DEBUG record dropped required field missing ... _event_time` or `FieldMapError` |
| Outcome is correct | `"outcome"` is `success`, `failure`, or `unknown` | `ValueError: invalid outcome` in traceback |
| Pagination works | Multiple pages of events appear | Only first page, then stops |

---

## Step 4 — Iterate with Claude Code

After the initial dry run, paste failures directly into Claude Code:

**Missing field:**
```
Running --dry-run --limit 5 produces this output:

  DEBUG record dropped required field missing collector=myapp_audit field=_native_actor_id

Here is a sample raw record from the DEBUG output:
  {"id": "evt_123", "userId": "u_abc", "timestamp": "...", ...}

The actor ID is in userId, not actor.id. Fix the fieldmap.
```

**Pagination not working:**
```
The collector only returns the first page. The API uses a nextPageToken
field in the response body. Here is the response shape:
  {"events": [...], "nextPageToken": "abc123"}

Fix poll_records() to follow pagination.
```

**New transform needed:**
```
The API returns outcome as an integer HTTP status code (200 = success,
4xx/5xx = failure). None of the existing FIELD_TRANSFORMS fit this.
Add a new transform http_status_to_outcome to field_mapping.py and
register it in FIELD_TRANSFORMS, then use it in the fieldmap.
```

---

## Step 5 — Promote to production

1. Replace `PassthroughCorrelator()` in `create_collector()` with your
   real `IdentityCorrelator` implementation that queries the IGA identity
   store:
   ```python
   from my_org.iga import IgaCorrelator
   correlator=IgaCorrelator(config["iga_base_url"])
   ```

2. Set `"enabled": true` in the `.json` config and fill in real credentials.

3. Mount the directory in Docker and run:
   ```bash
   docker run --rm --env-file .env \
     -v /path/to/collectors:/collectors \
     -v /path/to/state:/state \
     iga-collectors --collector <name>_collector
   ```

---

## Quick reference — common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| `_native_actor_id` points to a mutable field (email, display name) | Correlation breaks after user rename | Use the immutable GUID/numeric ID; email as fallback only |
| Timezone-naive timestamp | `ValueError: event_time must be timezone-aware` | Ensure transform returns a tz-aware datetime, or set `source_timezone=timezone.utc` in `create_collector()` |
| Overriding `poll()` instead of `poll_records()` | Checkpoint dedup and DEBUG logging stop working | Implement `poll_records()` only; `poll()` is the framework's |
| Credentials in `poll_records()` instead of `__init__` | Hard to test | Accept credentials in `__init__`, store on `self` |
| `create_collector()` missing or not callable | `WARNING skipping ...: does not define create_collector()` | Define `def create_collector(config: dict)` at module level |
| `filter_by_checkpoint=False` on a REST source | Events re-uploaded on every run | Only set this for Kafka/offset-managed sources; leave default for REST APIs |

---

## Appendix — Claude Code follow-up prompts

Use these verbatim after the initial bootstrap if Claude Code's output needs
adjustment.

**Fix actor ID path:**
```
The _native_actor_id in the fieldmap is wrong. The raw record looks like
this: <paste sample record>. The immutable actor ID is at <path>. Update
the fieldmap source_path for _native_actor_id.
```

**Add event type filter:**
```
poll_records() currently yields all events. Add a DEFAULT_EVENT_TYPES
frozenset containing <list event types> and filter in poll_records() —
same pattern as okta_collector.py. Also read the filter from
config.get("<name>_event_types") in create_collector() so it can be
overridden per-deployment.
```

**OAuth2 auth instead of API key:**
```
The API uses OAuth2 client_credentials, not a static API key. Replace the
manual Authorization header with iga_collectors.uploader.TokenClient —
same pattern as entra_collector.py or azure_collector.py. Token URL is
<url>, scope is <scope>. Accept client_id and client_secret in __init__
instead of api_key.
```

**Handle API-specific timestamp format:**
```
The timestamp field uses the format <describe format, e.g. Unix milliseconds,
"YYYYMMDD HH:MM:SS", etc.>. None of the existing FIELD_TRANSFORMS handle
this. Add a new transform called <name>_to_datetime to field_mapping.py,
register it in FIELD_TRANSFORMS, and use it in the fieldmap for _event_time.
```

**Capture nested events (one API item → many records):**
```
Each API response item contains an "events" array and each element should
become a separate activity record — same pattern as google_workspace_collector.py's
_activity_to_records(). Add a helper method that flattens one item into
multiple record dicts, call it from poll_records(), and yield the flattened
records.
```
