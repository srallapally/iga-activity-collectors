# CLAUDE.md — iga-collectors

## What this repo is

`iga-collectors` is a Python SDK and reference-collector framework for the IGA (Identity Governance & Administration) activity pipeline. It reads activity from external sources (databases, cloud APIs, log files, etc.), resolves each actor to an IGA identity, and uploads batches of canonical `ActivityLogEvent` records to the IGA governance activity API (`POST /iga/governance/activity?_action=upload`) as CSV + field-mapping document pairs. The framework is modeled on SailPoint IdentityIQ's activity monitoring abstractions.

## Layout

```
src/iga_collectors/      installable SDK package
  base.py                core abstractions (BaseCollector, TabularActivityCollector,
                          IdentityCorrelator, CheckpointStore, RawActivity, new_event_shell)
  field_mapping.py       declarative JSON-driven mapping (DeclarativeMappedCollector,
                          resolve_path, FIELD_TRANSFORMS)
  mapping.py             CSV + mapping-doc serialization (flatten_event, events_to_csv,
                          build_mapping_doc, build_upload_payload)
  uploader.py            OAuth2 token client + multipart upload (TokenClient,
                          ActivityUploader)
  config.py              env-var config loading (load_config, build_uploader,
                          build_collector_base_config)
  discovery.py           COLLECTORS_DIR scan, dynamic import, run_and_upload, run_all
  main.py / __main__.py  CLI entry point (iga-collectors); run-and-exit, not a daemon

examples/                reference collectors (NOT auto-discovered from here; copy to
                          an external COLLECTORS_DIR)
  acme/                  tutorial: fictional REST SaaS API (DeclarativeMappedCollector)
  activedirectory/       AD/LDAP
  aws/                   AWS CloudTrail
  azure/                 Azure Activity Log
  entra/                 Microsoft Entra ID (auditLogs/directoryAudits + signIns)
  google_cloud/          GCP Cloud Logging
  google_workspace/      Google Workspace Admin SDK
  jaeger/                Jaeger tracing API
  jdbc/                  Generic JDBC (TabularActivityCollector)
  logfile/               Log file regex (TabularActivityCollector)
  m365/                  Microsoft 365 (Office 365 Management API)
  okta/                  Okta System Log API
  otel/                  OpenTelemetry (OTLP/Kafka, filter_by_checkpoint=False)
  salesforce/            Salesforce EventLogFile CSV
  windows/               Windows Event Log
  unfinished/            RACF audit log (incomplete stub)

tests/
  test_base.py           STUB (placeholder only)
  test_mapping.py        STUB (placeholder only)
  test_uploader.py       STUB (placeholder only)
  test_discovery.py      STUB (placeholder only)
  fixtures/
    sample_collectors_dir/dummy_collector.py   fake collector for discovery tests
    testActivity.csv                           minimal synthetic fixture
```

## Build and test commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Install with all collector optional deps
pip install -e ".[all-collectors,dev]"

# Run tests
pytest

# Run the CLI (requires .env / env vars set)
iga-collectors
# or
python -m iga_collectors

# Docker (one-shot run; mount COLLECTORS_DIR and CHECKPOINT_STORE_PATH as volumes)
docker build -t iga-collectors .
docker run --env-file .env \
  -v /path/to/collectors:/collectors \
  -v /path/to/state:/state \
  iga-collectors
```

There is no linting configuration in this repo. No `.flake8`, `ruff.toml`, or `mypy.ini` exists as of initial commit.

## Architecture

### Core abstractions (base.py)

**`RawActivity`** (dataclass) — intermediate record after reading from source, before schema mapping. Fields: `native_user_id`, `action`, `target`, `event_time` (timezone-aware `datetime`), `result` (`"success"` | `"failure"` | `"unknown"` | `""`), `info`, `raw` (original source dict).

**`BaseCollector`** (ABC) — top-level base class. Subclasses implement three methods:
- `poll(since_position)` → `Iterator[RawActivity]`: yields records newer than checkpoint
- `next_position(activity)` → `str`: returns opaque checkpoint value (usually ISO timestamp)
- `map_to_event(activity, actor_global_id)` → `dict`: builds canonical event

`run()` orchestrates: reads checkpoint → polls → correlates → maps → advances checkpoint. Override `run()` only in extraordinary circumstances.

**`TabularActivityCollector`** (extends `BaseCollector`) — shared base for JDBC, log-file, RACF, and any other source that yields flat field-value dicts. Subclasses implement `poll_rows(since_position)` instead of `poll()`. Configured via `ColumnMap` (maps field names to `RawActivity` slots) and an optional `time_parser` callable.

**`DeclarativeMappedCollector`** (field_mapping.py, extends `BaseCollector`) — base for all REST API collectors. Subclasses implement `poll_records(since_position)` (API mechanics only: auth, pagination). Record-to-event mapping is fully data-driven by a `.fieldmap.json` document. Use this base for all new cloud/REST collectors.

**`IdentityCorrelator`** (ABC) — resolves `native_user_id` → `actor_global_id` (UUID). Only implementation provided is `PassthroughCorrelator` (returns native ID unchanged; logs a warning; NOT for production). Production use requires a concrete implementation querying the IGA identity store.

**`CheckpointStore`** — persists last-read position per collector to a JSON file keyed by `collector_id`. One file for all collectors; path set by `CHECKPOINT_STORE_PATH` env var. Position is opaque (ISO timestamp for all current collectors).

**`new_event_shell()`** — builds the required top-level fields of an `ActivityLogEvent`. Validates `outcome` ∈ `{"success","failure","partial","unknown"}` and that `event_time` is timezone-aware. Sets `id`, `schema_version` (`"3.0.0"`), `actor_global_id`, `event_id`, `event_time` (UTC ISO), `event_type`, `action`, `outcome`, `ingest_metadata.source_system`, `ingest_metadata.ingest_time`.

### Declarative field mapping (field_mapping.py)

`.fieldmap.json` document shape:
```json
{
  "source_system": "aws_cloudtrail",
  "event_type": "resource_access",
  "fields": {
    "_native_actor_id": {"source_path": "Username", "required": true},
    "_event_time": {"source_path": "EventTime", "required": true},
    "action": {"source_path": "EventName", "required": true},
    "outcome": {"source_path": "CloudTrailEvent.$json", "transform": "error_code_presence_to_outcome", "default": "unknown"},
    "resource.resource_name": {"source_path": ["Resources[0].ResourceName", "EventSource"], "default": "unknown_resource"}
  }
}
```

- `_native_actor_id` and `_event_time` are reserved; not written to the event
- All other keys are dotted canonical schema paths written via `set_dotted()` into the event dict
- `source_path` may be a string or list (first non-None wins)
- `$json` segment: parse the current value as a JSON string and continue
- `[N]` indexing: `Resources[0].ResourceName`
- `transform` names a key from `FIELD_TRANSFORMS`; `default` applies when resolved+transformed value is `None`; `required: true` drops the whole record if still `None`

**`FIELD_TRANSFORMS`** (registered callables):
`identity`, `parse_iso8601`, `bool_to_outcome`, `error_code_presence_to_outcome`, `access_key_to_credential_type`, `sailpoint_result_to_outcome`, `graph_signin_error_code_to_outcome`, `azure_status_to_outcome`, `o365_status_to_outcome`, `parse_salesforce_timestamp`, `gcp_status_code_to_outcome`, `unix_nanos_to_datetime`, `otel_status_code_to_outcome`, `micros_to_datetime`

`filter_by_checkpoint=False` on `DeclarativeMappedCollector` disables the post-poll timestamp dedup filter — required for Kafka/OTLP sources where the consumer manages its own offset state and clock skew is normal.

### Serialization (mapping.py)

`build_upload_payload(events)` → `(csv_text, mapping_doc)`:
- Flattens each event dict to `{dotted_path: cell_string}`
- CSV columns = union of all dotted paths across the batch, sorted
- Column header = dotted path (e.g. `actor.actor_type`)
- Cell encoding: `None`/missing → `""`, `bool` → `"true"`/`"false"`, `list` → JSON string, scalars → `str()`
- Mapping doc is the nested `{"column": "<path>"}` structure the upload API expects

### Upload (uploader.py)

`TokenClient` — OAuth2 `client_credentials` grant, caches token until 30 s before expiry. `ActivityUploader.upload(events)` calls `build_upload_payload`, then POSTs multipart: field `mapping` = JSON mapping doc, field `file` = CSV as `activity.csv`.

### Config (config.py)

`load_config()` reads from env vars (or a passed `Mapping`). All `REQUIRED_VARS` must be present or `ConfigError` is raised listing all missing vars at once.

Required env vars:
```
IGA_PROTOCOL, IGA_HOST, IGA_PORT, IGA_UPLOAD_PATH
IGA_TOKEN_URL, IGA_CLIENT_ID, IGA_CLIENT_SECRET
COLLECTORS_DIR, CHECKPOINT_STORE_PATH
```
Optional: `IGA_OAUTH_SCOPE`

### Discovery (discovery.py)

`run_all(directory, base_config, uploader)`:
1. Scans `COLLECTORS_DIR/*.py` (skips `_`-prefixed files)
2. For each file: loads optional sibling `{stem}.json` (per-collector credentials), merges over `base_config` (which contains `checkpoint_path`)
3. Calls `create_collector(config)` — the required module-level entry point
4. Validates result is a `BaseCollector`
5. Runs each collector via `run_and_upload()` in batches of 100
6. Isolates failures: one broken collector does not stop others

### Exit codes (main.py)
- `0` — all collectors succeeded
- `1` — fatal pre-run error (bad config, missing COLLECTORS_DIR)
- `2` — ran but ≥1 collector failed (partial failure)

## Conventions

### Writing a new collector

1. Create `{name}_collector.py` in your `COLLECTORS_DIR`
2. For REST/cloud sources: subclass `DeclarativeMappedCollector`, implement `poll_records(since_position)`, load field map from a sibling `{name}_collector.fieldmap.json`
3. For tabular sources (JDBC, log files): subclass `TabularActivityCollector`, implement `poll_rows(since_position)`
4. For non-standard pagination/state: subclass `BaseCollector` directly
5. Define `create_collector(config: dict) -> BaseCollector` at module level — this is the discovery entry point
6. Place per-collector credentials in a sibling `{name}_collector.json` (flat JSON object); keys in that file override `base_config` keys
7. The `config` dict always contains `checkpoint_path`; use it: `CheckpointStore(Path(config["checkpoint_path"]))`
8. Use `PassthroughCorrelator()` only for local testing; wire a real correlator for production
9. Checkpoint position is an ISO 8601 timestamp string for all current collectors; `next_position()` returns `activity.event_time.isoformat()`

### Canonical event schema

Schema version `"3.0.0"` (constant `SCHEMA_VERSION` in base.py). Required top-level fields: `id`, `schema_version`, `actor_global_id`, `event_id`, `event_time` (UTC ISO), `event_type`, `action`, `outcome`. The `outcome` enum is `success | failure | partial | unknown`. `event_time` must be timezone-aware before being passed to `new_event_shell()` — naive datetimes are rejected.

### Key invariants

- `event_time` must always carry `tzinfo`; `TabularActivityCollector` and `DeclarativeMappedCollector` attach `source_timezone` when the raw value is naive
- `actor_global_id` must be an IGA UUID in production; `PassthroughCorrelator` is a stub that returns the native ID and logs a warning
- `CHECKPOINT_STORE_PATH` must be a persistent path (volume mount in Docker); ephemeral storage causes every restart to re-upload already-seen events
- Files starting with `_` in `COLLECTORS_DIR` are skipped — use this for shared helper modules
- A collector file that lacks `create_collector` or whose factory raises is skipped with a warning, not a hard stop
- Arrays in event dicts are JSON-encoded as strings in CSV (e.g. `'["role1","role2"]'`)
- `mapping.py` never transforms values — all CSV cells must already be schema-valid strings

### Module implementation status

| Module | Status |
|---|---|
| `base.py` | Implemented |
| `field_mapping.py` | Implemented |
| `mapping.py` | Implemented |
| `uploader.py` | Implemented |
| `config.py` | Implemented |
| `discovery.py` | Implemented |
| `main.py` / `__main__.py` | Implemented |
| `tests/test_base.py` | STUB |
| `tests/test_mapping.py` | STUB |
| `tests/test_uploader.py` | STUB |
| `tests/test_discovery.py` | STUB |
| `examples/unfinished/racf_audit_log_collector.py` | STUB |
| All other `examples/` collectors | Implemented |
