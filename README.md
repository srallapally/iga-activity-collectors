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

| Module                          | Status      |
|----------------------------------|-------------|
| `iga_collectors.base`            | Implemented |
| `iga_collectors.mapping`         | Implemented |
| `iga_collectors.uploader`        | Stub — pending |
| `iga_collectors.discovery`       | Stub — pending |
| `iga_collectors.config`          | Stub — pending |
| `examples/jdbc_collector`         | Stub — pending |
| `examples/log_file_collector`     | Stub — pending |
| `examples/racf_audit_log_collector` | Stub — pending |
| `examples/windows_eventlog_collector` | Stub — pending |
| `examples/entra_collector`        | Stub — pending |
| `examples/azure_collector`        | Stub — pending |
| `examples/office365_collector`    | Stub — pending |
| `examples/google_workspace_collector` | Stub — pending |
| `examples/salesforce_collector`   | Stub — pending |
| `examples/aws_collector`          | Stub — pending |
| `examples/google_cloud_collector` | Stub — pending |
| `examples/okta_collector`         | Stub — pending |
| `examples/otel_collector`         | Stub — pending |

Note: `tests/fixtures/testActivity.csv` is a minimal synthetic fixture, not
the real file from the original API example (its content was never
provided) — replace it with a real sample when available.

## Layout

```
src/iga_collectors/   the framework (pip-installable)
examples/              reference collectors (not auto-discovered from here)
tests/                 unit tests + fixtures, including a fake COLLECTORS_DIR
```

## PyCharm setup

1. Open this folder as a PyCharm project.
2. Mark `src` as **Sources Root** (right-click → Mark Directory as).
3. Mark `tests` as **Test Sources Root**.
4. Set the project interpreter to a venv with `pip install -e ".[dev]"`.
5. Set the default test runner to pytest (Settings → Tools → Python Integrated Tools).

## Configuration

Copy `.env.example` to `.env` and fill in real values before running any
collector against a live IGA instance.
