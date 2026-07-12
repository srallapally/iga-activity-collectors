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

| Module                                    | Status          |
|-------------------------------------------|-----------------|
| `iga_collectors.base`                     | Implemented     |
| `iga_collectors.field_mapping`            | Implemented     |
| `iga_collectors.mapping`                  | Implemented     |
| `iga_collectors.uploader`                 | Implemented     |
| `iga_collectors.config`                   | Implemented     |
| `iga_collectors.discovery`                | Implemented     |
| `examples/acme`                           | Implemented     |
| `examples/activedirectory`                | Implemented     |
| `examples/aws`                            | Implemented     |
| `examples/azure`                          | Implemented     |
| `examples/entra`                          | Implemented     |
| `examples/google_cloud`                   | Implemented     |
| `examples/google_workspace`               | Implemented     |
| `examples/jaeger`                         | Implemented     |
| `examples/jdbc`                           | Implemented     |
| `examples/logfile`                        | Implemented     |
| `examples/m365`                           | Implemented     |
| `examples/okta`                           | Implemented     |
| `examples/otel`                           | Implemented     |
| `examples/salesforce`                     | Implemented     |
| `examples/windows`                        | Implemented     |
| `examples/unfinished/racf_audit_log_collector` | Stub — pending |

Note: `tests/fixtures/testActivity.csv` is a minimal synthetic fixture, not
the real file from the original API example (its content was never
provided) — replace it with a real sample when available.

## Layout

```
src/iga_collectors/   the framework (pip-installable)
examples/              reference collectors (not auto-discovered from here)
tests/                 unit tests + fixtures, including a fake COLLECTORS_DIR
```

## Usage

### Install

```bash
pip install -e "."
# With all collector optional dependencies:
pip install -e ".[all-collectors]"
```

### Run all collectors

Discovers and runs every collector in `COLLECTORS_DIR`, uploads results, and exits:

```bash
iga-collectors
# or
python -m iga_collectors
```

### List deployed collectors

Prints the name of each collector found in `COLLECTORS_DIR` without requiring IGA credentials:

```bash
COLLECTORS_DIR=/path/to/collectors iga-collectors --list
```

### Run a specific collector

Runs a single named collector instead of the full fleet:

```bash
iga-collectors --collector okta_collector
```

If the name is not found, the error message includes a hint to use `--list`:

```
ERROR: no collector named 'okta_collector' in /path/to/collectors — use --list to see available collectors
```

### COLLECTORS_DIR layout

Each collector lives in its own subdirectory. Copy the three files from `examples/<name>/` into your `COLLECTORS_DIR`:

```
COLLECTORS_DIR/
  okta/
    okta_collector.py
    okta_collector.fieldmap.json
    okta_collector.json          # credentials and config
  entra/
    entra_collector.py
    entra_collector.fieldmap.json
    entra_collector.json
```

### Docker (one-shot)

```bash
docker build -t iga-collectors .
docker run --rm --env-file .env \
  -v /path/to/collectors:/collectors \
  -v /path/to/state:/state \
  iga-collectors
```

### Scheduling

The process is one-shot by design — it runs once and exits. Use an external scheduler:

- **cron**: `*/15 * * * * root /usr/local/bin/docker-run.sh`
- **Kubernetes**: see `deployment/k8s-cronjob.yaml` (includes `concurrencyPolicy: Forbid` and a `PersistentVolumeClaim` for checkpoint state — both required for correctness)

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | All collectors succeeded |
| `1` | Fatal error before any collector ran (bad config, missing `COLLECTORS_DIR`) |
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
| `COLLECTORS_DIR` | Path to directory containing collector `.py` files |
| `CHECKPOINT_STORE_PATH` | Path to the checkpoint state file (must be on persistent storage) |

### Optional

| Variable | Purpose |
|---|---|
| `IGA_OAUTH_SCOPE` | OAuth2 scope string |
