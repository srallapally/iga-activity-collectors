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

## Configuration

Copy `.env.example` to `.env` and fill in real values before running any
collector against a live IGA instance.
