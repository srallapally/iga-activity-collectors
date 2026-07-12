# iga-collectors

Activity collector framework for the IGA governance activity pipeline.

A collector reads activity from some source (database, log file, cloud API,
...), resolves each actor to an IGA identity, and produces canonical
`ActivityLogEvent` records. The framework flattens a batch of those events to
CSV, builds the matching field-mapping document, and uploads both to:

    POST {{protocol}}://{{host}}:{{port}}/iga/governance/activity?_action=upload

Collectors are customer-written and live **outside this repo**, in a directory
pointed to by `COLLECTORS_DIR`. This repo is the SDK (`src/iga_collectors/`)
plus reference implementations (`examples/`) that customers copy as a starting
point.

## Documentation

| Topic | Link |
|---|---|
| Collector event coverage, actor IDs, config keys | [docs/collectors.md](docs/collectors.md) |
| Logging levels, per-run summary, lifecycle sequence | [docs/logging.md](docs/logging.md) |
| Building a new collector with Claude Code | [DEVELOPER_COOKBOOK.md](DEVELOPER_COOKBOOK.md) |

## Status

| Module | Status |
|---|---|
| `iga_collectors.base` | Implemented |
| `iga_collectors.field_mapping` | Implemented |
| `iga_collectors.mapping` | Implemented |
| `iga_collectors.uploader` | Implemented |
| `iga_collectors.config` | Implemented |
| `iga_collectors.discovery` | Implemented |
| `iga_collectors.logging_setup` | Implemented |
| `examples/acme` | Implemented |
| `examples/activedirectory` | Implemented |
| `examples/aws` | Implemented |
| `examples/azure` | Implemented |
| `examples/entra` | Implemented |
| `examples/google_cloud` | Implemented |
| `examples/google_workspace` | Implemented |
| `examples/jaeger` | Implemented |
| `examples/jdbc` | Implemented |
| `examples/logfile` | Implemented |
| `examples/m365` | Implemented |
| `examples/okta` | Implemented |
| `examples/otel` | Implemented |
| `examples/salesforce` | Implemented |
| `examples/windows` | Implemented |
| `examples/unfinished/racf_audit_log_collector` | Stub — pending |

## Layout

```
src/iga_collectors/   the framework (pip-installable)
examples/             reference collectors (not auto-discovered from here)
docs/                 reference documentation
tests/                unit tests + fixtures, including a fake COLLECTORS_DIR
```

## Install

```bash
pip install -e "."
# With all collector optional dependencies:
pip install -e ".[all-collectors]"
```

## CLI reference

```
iga-collectors [--list] [--collector NAME] [--dry-run] [--limit N]
```

| Flag | Env var equivalent | Requires IGA creds | Description |
|---|---|---|---|
| _(none)_ | — | Yes | Run all enabled collectors and upload |
| `--list` | — | No | List collectors in `COLLECTORS_DIR` with enabled/disabled status |
| `--collector NAME` | — | Yes | Run one named collector instead of all |
| `--dry-run` | `DRY_RUN=true` | No | Poll and map events, print to stdout — skip upload entirely |
| `--dry-run --collector NAME` | `DRY_RUN=true` + `--collector` | No | Dry run a single collector |
| `--limit N` | `LIMIT=N` | — | Stop each collector after N events; works with or without `--dry-run` |

## COLLECTORS_DIR layout

Each collector lives in its own subdirectory. Copy the three files from
`examples/<name>/` into your `COLLECTORS_DIR`, edit the `.json` config with
real credentials, and set `"enabled": true`:

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

All example `.json` configs ship with `"enabled": false`. A collector with no
`.json` config, or whose config omits `"enabled"`, is treated as enabled.

Three keys control runtime behaviour in any collector's `.json` config:

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `true` | `false` skips this collector entirely |
| `dry_run` | bool | `false` | `true` runs the full pipeline but prints events to stdout instead of uploading |
| `log_level` | string | _(inherit)_ | Overrides `LOG_LEVEL` for this collector only — `DEBUG` prints raw records and mapped event dicts |

## Test mode (dry run)

`--dry-run` runs the full pipeline (poll → correlate → map) but prints events
as JSON to stdout instead of uploading. **IGA credentials are not required.**
Checkpoint state is never read or written.

```bash
# Test one collector — first 5 events, no IGA creds needed
COLLECTORS_DIR=/path/to/collectors \
  iga-collectors --dry-run --limit 5 --collector okta_collector

# Dry run all enabled collectors, 10 events each
COLLECTORS_DIR=/path/to/collectors iga-collectors --dry-run --limit 10
```

| Tested by dry-run | Not tested |
|---|---|
| Source credentials (API key, token) | IGA upload endpoint reachability |
| API pagination | OAuth2 client credentials to IGA |
| Field mapping (`fieldmap.json` correctness) | Mapping doc format accepted by IGA |
| Identity correlation | Checkpoint write-back |

## Docker (one-shot)

```bash
docker build -t iga-collectors .
docker run --rm --env-file .env \
  -v /path/to/collectors:/collectors \
  -v /path/to/state:/state \
  iga-collectors
```

`COLLECTORS_DIR=/collectors` and `CHECKPOINT_STORE_PATH=/state/checkpoint.json`
are baked into the image — only override them if you mount to different paths.

To update after a `git pull`: `docker build -t iga-collectors .`

## Scheduling

The process is one-shot by design — it runs once and exits. Use an external
scheduler:

- **cron**: `*/15 * * * * root /usr/local/bin/docker-run.sh`
- **Kubernetes**: `CronJob` with `concurrencyPolicy: Forbid` and a
  `PersistentVolumeClaim` for the state volume (required for checkpoint
  correctness)

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All collectors succeeded (or dry run completed) |
| `1` | Fatal error before any collector ran (bad config, missing `COLLECTORS_DIR`, disabled collector named explicitly) |
| `2` | At least one collector failed; others may have succeeded |

## Configuration

Copy `.env.example` to `.env` and fill in values before running against a live
IGA instance.

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
| `COLLECTORS_DIR` | Path to collector subdirectories (baked into Docker image as `/collectors`) |
| `CHECKPOINT_STORE_PATH` | Path to checkpoint state file — must be on persistent storage (baked into Docker image as `/state/checkpoint.json`) |

### Optional environment variables

| Variable | Default | Purpose |
|---|---|---|
| `IGA_OAUTH_SCOPE` | _(none)_ | OAuth2 scope string |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` — see [docs/logging.md](docs/logging.md) |
| `LOG_FORMAT` | `text` | `text` or `json` (for ELK, Datadog, CloudWatch, Splunk) |
| `DRY_RUN` | `false` | `true` prints events to stdout instead of uploading |
| `LIMIT` | _(none)_ | Stop each collector after N events |

CLI flags (`--dry-run`, `--limit N`) take precedence over env vars when both
are set.
