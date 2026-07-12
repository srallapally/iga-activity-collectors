# Logging Reference

The framework emits structured logs across all layers. Set `LOG_LEVEL=DEBUG`
to see the full picture; set `LOG_FORMAT=json` to feed logs into an
aggregator (ELK, Datadog, CloudWatch, Splunk).

## Log levels

| Level | Examples |
|---|---|
| `DEBUG` | Checkpoint load/save, per-record correlation (`native_id → global_id`), raw API records, mapped event dicts, upload request size, token cache hits |
| `INFO` | Collector start/complete with duration, token refresh with expiry time, upload accepted, disabled collector skipped, per-run summary |
| `WARNING` | Token request failure (with HTTP status), collector load failure, `PassthroughCorrelator` in use, optional stream unavailable (e.g. Entra SP sign-ins without Premium license) |
| `ERROR` | Upload failure (with HTTP status and first 500 characters of response body) |

## Per-run summary

Every run ends with a single summary line on `INFO`:

```
run_complete collectors_run=3 collectors_skipped=13 collectors_failed=0 events_uploaded=247 duration_s=4.2
```

In `LOG_FORMAT=json` mode:

```json
{"time": "2026-07-12T10:18:14.042Z", "level": "INFO", "logger": "iga_collectors", "msg": "run_complete collectors_run=3 collectors_skipped=13 collectors_failed=0 events_uploaded=247 duration_s=4.2"}
```

## Token and upload observability

The token client logs each refresh and cache hit without logging the
`client_secret` or the access token value itself. Upload requests log the
event count and CSV size at `DEBUG`; failures log the HTTP status and the
first 500 characters of the response body at `ERROR`.

## Collector lifecycle sequence

```
INFO  collector starting collector=okta_collector since=2026-07-12T09:00:00+00:00
DEBUG checkpoint loaded collector=okta_collector position=2026-07-12T09:00:00+00:00
DEBUG poll complete collector=okta_collector records_yielded=42 since=2026-07-12T09:00:00+00:00
INFO  collector complete collector=okta_collector events=42 duration_s=1.3
DEBUG checkpoint saved collector=okta_collector position=2026-07-12T10:00:00+00:00
```

## Per-collector log level override

Set `log_level` in a collector's `.json` config to override the process-wide
`LOG_LEVEL` for that collector's run only. Useful for debugging one collector
at `DEBUG` without flooding logs from the others:

```json
{
  "enabled": true,
  "log_level": "DEBUG",
  "okta_org_url": "https://myorg.okta.com",
  "okta_api_token": "..."
}
```

The override is scoped to the collector's `run()` call and restored
automatically on exit, even if the collector raises.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Process-wide verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | `text` | `text` for human-readable; `json` for structured log aggregation |
