# iga-collectors

Activity collector framework for the IGA governance activity pipeline.

A collector reads activity from some source (database, log file, cloud API,
...), resolves each actor to an IGA identity, and produces canonical
`ActivityLogEvent` records. The framework then flattens a batch of those
events to CSV, builds the matching field-mapping document, and uploads both
to:

    POST {{protocol}}://{{host}}:{{port}}/iga/governance/activity?_action=upload

Collectors are customer-written and live **outside this repo**, in a
directory pointed to by `COLLECTORS_DIR` (see `.env.example`). This repo is
the SDK (`src/iga_collectors/`) plus reference implementations
(`examples/`) that customers can copy as a starting point.

## Status

| Module                                         | Status          |
|------------------------------------------------|-----------------|
| `iga_collectors.base`                          | Implemented     |
| `iga_collectors.field_mapping`                 | Implemented     |
| `iga_collectors.mapping`                       | Implemented     |
| `iga_collectors.uploader`                      | Implemented     |
| `iga_collectors.config`                        | Implemented     |
| `iga_collectors.discovery`                     | Implemented     |
| `iga_collectors.logging_setup`                 | Implemented     |
| `examples/acme`                                | Implemented     |
| `examples/activedirectory`                     | Implemented     |
| `examples/aws`                                 | Implemented     |
| `examples/azure`                               | Implemented     |
| `examples/entra`                               | Implemented     |
| `examples/google_cloud`                        | Implemented     |
| `examples/google_workspace`                    | Implemented     |
| `examples/jaeger`                              | Implemented     |
| `examples/jdbc`                                | Implemented     |
| `examples/logfile`                             | Implemented     |
| `examples/m365`                                | Implemented     |
| `examples/okta`                                | Implemented     |
| `examples/otel`                                | Implemented     |
| `examples/salesforce`                          | Implemented     |
| `examples/windows`                             | Implemented     |
| `examples/unfinished/racf_audit_log_collector` | Stub — pending  |

## Layout

```
src/iga_collectors/   the framework (pip-installable)
examples/             reference collectors (not auto-discovered from here)
tests/                unit tests + fixtures, including a fake COLLECTORS_DIR
```

## Usage

### Install

```bash
pip install -e "."
# With all collector optional dependencies:
pip install -e ".[all-collectors]"
```

### CLI reference

```
iga-collectors [--list] [--collector NAME] [--dry-run] [--limit N]
```

| Flag | Requires IGA creds | Description |
|---|---|---|
| _(none)_ | Yes | Run all enabled collectors and upload |
| `--list` | No | List collectors in `COLLECTORS_DIR` with enabled/disabled status |
| `--collector NAME` | Yes | Run one named collector instead of all |
| `--dry-run` | No | Poll and map events, print to stdout — skip upload entirely |
| `--dry-run --collector NAME` | No | Dry run a single collector |
| `--limit N` | — | Stop each collector after N events; works with or without `--dry-run` |

### COLLECTORS_DIR layout

Each collector lives in its own subdirectory. Copy the three files from `examples/<name>/` into your `COLLECTORS_DIR`, then edit the `.json` config with real credentials and set `"enabled": true`:

```
COLLECTORS_DIR/
  okta/
    okta_collector.py
    okta_collector.fieldmap.json
    okta_collector.json          # credentials, config, and "enabled" flag
  entra/
    entra_collector.py
    entra_collector.fieldmap.json
    entra_collector.json
```

All example `.json` configs ship with `"enabled": false`. A collector that has no `.json` config, or whose config omits `"enabled"`, is treated as enabled.

### List deployed collectors

Prints each collector with a `[disabled]` marker for unconfigured ones. Does not require IGA credentials:

```bash
COLLECTORS_DIR=/path/to/collectors iga-collectors --list
okta_collector  [disabled]
entra_collector
```

### Run all collectors

Discovers and runs every enabled collector in `COLLECTORS_DIR`, uploads results, and exits:

```bash
iga-collectors
# or
python -m iga_collectors
```

### Run a specific collector

```bash
iga-collectors --collector entra_collector
```

Error messages if something is wrong:

```
# Collector exists but is disabled:
ERROR: collector 'okta_collector' is disabled — set "enabled": true in okta_collector.json to run it

# Collector not found:
ERROR: no collector named 'okta_collector' in /path/to/collectors — use --list to see available collectors
```

### Test mode (dry run)

`--dry-run` runs the full pipeline (poll → correlate → map) but prints events to stdout as JSON instead of uploading. **IGA credentials are not required.** Checkpoint state is never read or written — the run always starts from each collector's `initial_lookback_seconds` default.

```bash
# Test one collector — print first 5 events, no IGA creds needed
COLLECTORS_DIR=/path/to/collectors \
  iga-collectors --dry-run --limit 5 --collector okta_collector

# Dry run all enabled collectors, 10 events each
COLLECTORS_DIR=/path/to/collectors iga-collectors --dry-run --limit 10
```

`--limit` also works on live runs to cap events uploaded per collector:

```bash
iga-collectors --limit 100 --collector entra_collector
```

**What dry-run tests vs. what it doesn't:**

| Tested | Not tested |
|---|---|
| Source credentials (API key, token) | IGA upload endpoint reachability |
| API pagination | OAuth2 client credentials to IGA |
| Field mapping (`fieldmap.json` correctness) | Mapping doc format accepted by IGA |
| Identity correlation | Checkpoint write-back |

### Docker (one-shot)

```bash
docker build -t iga-collectors .
docker run --rm --env-file .env \
  -v /path/to/collectors:/collectors \
  -v /path/to/state:/state \
  iga-collectors
```

`COLLECTORS_DIR=/collectors` and `CHECKPOINT_STORE_PATH=/state/checkpoint.json` are baked into the image — only override them if you mount to different paths.

### Scheduling

The process is one-shot by design — it runs once and exits. Use an external scheduler:

- **cron**: `*/15 * * * * root /usr/local/bin/docker-run.sh`
- **Kubernetes**: `CronJob` with `concurrencyPolicy: Forbid` and a `PersistentVolumeClaim` for the state volume (required for checkpoint correctness)

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | All collectors succeeded (or dry run completed) |
| `1` | Fatal error before any collector ran (bad config, missing `COLLECTORS_DIR`, disabled collector named explicitly) |
| `2` | At least one collector failed; others may have succeeded |

## Configuration

Copy `.env.example` to `.env` and fill in real values before running any
collector against a live IGA instance.

### Required environment variables

| Variable | Purpose |
|---|---|
| `IGA_PROTOCOL` | `https` or `http` |
| `IGA_HOST` | IGA server hostname |
| `IGA_PORT` | IGA server port |
| `IGA_UPLOAD_PATH` | Upload endpoint path |
| `IGA_TOKEN_URL` | OAuth2 token endpoint URL |
| `IGA_CLIENT_ID` | OAuth2 client ID |
| `IGA_CLIENT_SECRET` | OAuth2 client secret |
| `COLLECTORS_DIR` | Path to directory containing collector subdirectories (baked into Docker image as `/collectors`) |
| `CHECKPOINT_STORE_PATH` | Path to the checkpoint state file — must be on persistent storage (baked into Docker image as `/state/checkpoint.json`) |

### Optional

| Variable | Default | Purpose |
|---|---|---|
| `IGA_OAUTH_SCOPE` | _(none)_ | OAuth2 scope string |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `LOG_FORMAT` | `text` | `text` for human-readable output; `json` for structured log aggregation (ELK, Datadog, CloudWatch, Splunk) |

## Logging

The framework logs across all layers. Set `LOG_LEVEL=DEBUG` to see the full
picture; use `LOG_FORMAT=json` to feed logs into an aggregator.

### What is logged at each level

| Level | Examples |
|---|---|
| `DEBUG` | Checkpoint load/save, per-record correlation (`native_id → global_id`), poll record counts, upload request size, token cache hits |
| `INFO` | Collector start/complete with duration, token refresh with expiry time, upload accepted, disabled collector skipped, per-run summary |
| `WARNING` | Token request failure (with HTTP status), collector load failure |
| `ERROR` | Upload failure (with HTTP status and truncated response body) |

### Per-run summary line

Every run ends with a single summary line that is easy to alert on:

```
run_complete collectors_run=3 collectors_skipped=13 collectors_failed=0 events_uploaded=247 duration_s=4.2
```

In `LOG_FORMAT=json` mode this becomes a structured object:

```json
{"time": "2026-07-12T10:18:14.042Z", "level": "INFO", "logger": "iga_collectors", "msg": "run_complete collectors_run=3 ..."}
```

### Token and upload observability

The token client logs each refresh and cache hit without ever logging the
`client_secret` or the access token value itself. Upload requests log the
event count and CSV size at DEBUG; failures include the HTTP status and the
first 500 characters of the response body.

### Collector lifecycle

```
INFO  collector starting collector=okta_collector since=2026-07-12T09:00:00+00:00
DEBUG checkpoint loaded collector=okta_collector position=2026-07-12T09:00:00+00:00
DEBUG poll complete collector=okta_collector records_yielded=42 since=2026-07-12T09:00:00+00:00
INFO  collector complete collector=okta_collector events=42 duration_s=1.3
DEBUG checkpoint saved collector=okta_collector position=2026-07-12T10:00:00+00:00
```
