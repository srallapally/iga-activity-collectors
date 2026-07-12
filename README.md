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

| Flag | Env var equivalent | Requires IGA creds | Description |
|---|---|---|---|
| _(none)_ | — | Yes | Run all enabled collectors and upload |
| `--list` | — | No | List collectors in `COLLECTORS_DIR` with enabled/disabled status |
| `--collector NAME` | — | Yes | Run one named collector instead of all |
| `--dry-run` | `DRY_RUN=true` | No | Poll and map events, print to stdout — skip upload entirely |
| `--dry-run --collector NAME` | `DRY_RUN=true` + `--collector` | No | Dry run a single collector |
| `--limit N` | `LIMIT=N` | — | Stop each collector after N events; works with or without `--dry-run` |

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

Three additional keys can appear in any collector's `.json` config:

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `true` | `false` skips this collector entirely |
| `dry_run` | bool | `false` | `true` runs the full pipeline but prints events to stdout instead of uploading to IGA |
| `log_level` | string | _(inherit)_ | Overrides `LOG_LEVEL` for this collector's run only — `DEBUG` prints raw API records and mapped event dicts |

`dry_run` and `log_level` are per-collector; the global `--dry-run` / `DRY_RUN` and `LOG_LEVEL` settings remain unchanged for other collectors in the same run.

Example — a collector configured for debug output and local testing:

```json
{
  "enabled": true,
  "dry_run": true,
  "log_level": "DEBUG",
  "okta_org_url": "https://myorg.okta.com",
  "okta_api_token": "REPLACE_ME"
}
```

`--list` shows markers for the active mode:

```
okta_collector  [dry-run]
entra_collector
ad_collector    [disabled]
```

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

#### Example: per-collector dry run with event output

The run below has `azure_collector.json` and `entra_collector.json` both set to `"enabled": true, "dry_run": true`. All other collectors are disabled. Events are printed to stdout; nothing is uploaded to IGA.

```
$ docker run --rm --env-file ../.env \
    -v /path/to/examples:/collectors \
    -v /path/to/state:/state \
    iga-collectors

--- DRY RUN: events printed to stdout, nothing uploaded ---
--- limit: 5 event(s) per collector ---
INFO  skipping acme_directory_collector.py: disabled in config
INFO  skipping ad_collector.py: disabled in config
INFO  skipping aws_collector.py: disabled in config
...
INFO  collector azure_collector: dry_run=true, skipping upload
INFO  collector starting collector=azure_activity_log since=beginning
INFO  token refreshed expires_at=2026-07-12T19:33:16+00:00
INFO  collector complete collector=azure_activity_log events=0 duration_s=1.4
INFO  collector entra_collector: dry_run=true, skipping upload
INFO  collector starting collector=entra_directory_audits since=beginning
INFO  token refreshed expires_at=2026-07-12T19:33:18+00:00
WARNING  PassthroughCorrelator used for source_system=entra_id native_id=user@example.com
{
  "id": "bd4d0414-8d2f-4fe0-99cf-3265b0e9f9e0",
  "schema_version": "3.0.0",
  "actor_global_id": "user@example.com",
  "event_id": "bd4d0414-8d2f-4fe0-99cf-3265b0e9f9e0",
  "event_time": "2026-07-12T18:17:14.696680+00:00",
  "event_type": "resource_access",
  "action": "Validate user authentication",
  "outcome": "success",
  "ingest_metadata": {
    "source_system": "entra_id",
    "ingest_time": "2026-07-12T18:33:49+00:00"
  },
  "resource": {
    "resource_name": "00000000-0000-0000-0000-000000000000"
  }
}
{
  "id": "607d0a4d-7510-43f3-9bc1-413079e242f0",
  "schema_version": "3.0.0",
  "actor_global_id": "user@example.com",
  "event_id": "607d0a4d-7510-43f3-9bc1-413079e242f0",
  "event_time": "2026-07-12T18:18:21.661948+00:00",
  "event_type": "resource_access",
  "action": "Update service principal",
  "outcome": "success",
  "ingest_metadata": {
    "source_system": "entra_id",
    "ingest_time": "2026-07-12T18:33:49+00:00"
  },
  "resource": {
    "resource_name": "Ping Client"
  }
}
```

The `PassthroughCorrelator` warning is expected during testing — `actor_global_id` will be the native identity (email) rather than an IGA UUID until a real correlator is wired up.

## Collector reference

Each collector's `DEFAULT_*` filter set and actor ID field are documented below. All filters are configurable via the per-collector `.json` config — set the corresponding key to a list to override.

### AWS CloudTrail (`aws_collector`)

**Actor ID:** `userIdentity.principalId` (immutable; covers IAM users, assumed roles, federated identities)  
**Config key:** `aws_event_names`

| Category | Events captured |
|---|---|
| IAM user lifecycle | `CreateUser`, `DeleteUser`, `UpdateUser`, `CreateLoginProfile`, `DeleteLoginProfile`, `UpdateLoginProfile`, `CreateVirtualMFADevice`, `DeactivateMFADevice`, `EnableMFADevice` |
| IAM credentials | `CreateAccessKey`, `DeleteAccessKey`, `UpdateAccessKey` |
| Group membership | `AddUserToGroup`, `RemoveUserFromGroup`, `CreateGroup`, `DeleteGroup` |
| Policy management | `AttachUserPolicy`, `DetachUserPolicy`, `AttachRolePolicy`, `DetachRolePolicy`, `AttachGroupPolicy`, `DetachGroupPolicy`, `PutUserPolicy`, `DeleteUserPolicy`, `PutRolePolicy`, `DeleteRolePolicy` |
| Role lifecycle | `CreateRole`, `DeleteRole`, `UpdateRole`, `UpdateAssumeRolePolicy`, `CreateServiceLinkedRole`, `DeleteServiceLinkedRole` |
| Authentication | `ConsoleLogin`, `AssumeRole`, `AssumeRoleWithWebIdentity`, `AssumeRoleWithSAML` |
| Workload identity federation | `CreateOpenIDConnectProvider`, `DeleteOpenIDConnectProvider`, `UpdateOpenIDConnectProviderThumbprint`, `AddClientIDToOpenIDConnectProvider`, `RemoveClientIDFromOpenIDConnectProvider`, `CreateSAMLProvider`, `DeleteSAMLProvider`, `UpdateSAMLProvider` |

### Azure ARM (`azure_collector`)

**Actor ID:** `claims.oid` (AAD object ID — immutable for users, service principals, and managed identities; falls back to `caller`)  
**Config key:** `azure_operation_names`

| Category | Operations captured |
|---|---|
| RBAC role assignments | `roleAssignments/write`, `roleAssignments/delete` |
| Custom role definitions | `roleDefinitions/write`, `roleDefinitions/delete` |
| Classic administrators | `classicAdministrators/write`, `classicAdministrators/delete` |
| Policy assignments | `policyAssignments/write`, `policyAssignments/delete` |
| Managed identity lifecycle | `userAssignedIdentities/write`, `userAssignedIdentities/delete` |
| Workload identity federation | `userAssignedIdentities/federatedIdentityCredentials/write`, `.../federatedIdentityCredentials/delete` |

### Microsoft Entra ID (`entra_collector`)

**Actor ID:** `initiatedBy.user.id` / `initiatedBy.servicePrincipal.id` (audit); `userId` (user sign-ins); `servicePrincipalId` (SP sign-ins) — all immutable AAD object IDs

Runs three independent sub-streams, each with its own checkpoint:

| Stream | Checkpoint key | Coverage |
|---|---|---|
| Directory audits | `entra_directory_audits` | Account/group/app lifecycle changes — all audit log categories |
| User sign-ins | `entra_sign_ins` | Interactive and non-interactive user authentication |
| Service principal sign-ins | `entra_sp_sign_ins` | Daemon/app OAuth2 flows (`client_credentials`, certificate auth) |

Required Graph API permissions (application, admin consent): `AuditLog.Read.All`, `Directory.Read.All`

### Microsoft 365 (`office365_collector`)

**Actor ID:** `UserId` (as logged by M365; may be a UPN or a synthetic app identity for workload events)  
**Config key:** `o365_operation_names` (default: no filter — all operations in the content type)  
**Config key:** `o365_content_type` (default: `Audit.Exchange`)

Available content types: `Audit.Exchange`, `Audit.SharePoint`, `Audit.General`, `Audit.AzureActiveDirectory`. Set `o365_content_type` in the per-collector `.json` to switch. Note: `Audit.AzureActiveDirectory` overlaps with the Entra collector — enable one or the other, not both.

### Okta System Log (`okta_collector`)

**Actor ID:** `actor.id` (Okta internal ID — immutable; falls back to `actor.alternateId`)  
**Config key:** `okta_event_types`

| Category | Event types captured |
|---|---|
| Authentication | `user.session.start` |
| User lifecycle | `user.lifecycle.create`, `activate`, `deactivate`, `suspend`, `unsuspend`, `delete` |
| User account changes | `user.account.lock`, `privilege.grant`, `privilege.revoke`, `update_profile` |
| Group membership | `group.user_membership.add`, `group.user_membership.remove` |
| Application lifecycle | `application.lifecycle.create`, `update`, `delete`, `activate`, `deactivate` |
| OAuth2 grants/revocations | `app.oauth2.as.token.grant`, `app.oauth2.as.token.revoke` |
| API token management | `system.api_token.create`, `system.api_token.revoke` |

### GCP Cloud Audit Logs (`google_cloud_collector`)

**Actor ID:** `protoPayload.authenticationInfo.principalEmail` (service account email or user email — no immutable numeric ID available from this API)  
**Config key:** `gcp_method_names`

| Category | Methods captured |
|---|---|
| IAM policy | `SetIamPolicy` |
| Service account lifecycle | `CreateServiceAccount`, `DeleteServiceAccount`, `UndeleteServiceAccount`, `EnableServiceAccount`, `DisableServiceAccount`, `PatchServiceAccount`, `UpdateServiceAccount` |
| Service account keys | `CreateServiceAccountKey`, `DeleteServiceAccountKey`, `UploadServiceAccountKey` |
| Workload identity federation | `CreateWorkloadIdentityPool`, `UpdateWorkloadIdentityPool`, `DeleteWorkloadIdentityPool`, `UndeleteWorkloadIdentityPool`, `CreateWorkloadIdentityPoolProvider`, `UpdateWorkloadIdentityPoolProvider`, `DeleteWorkloadIdentityPoolProvider`, `UndeleteWorkloadIdentityPoolProvider` |
| Custom roles | `CreateRole`, `UpdateRole`, `DeleteRole`, `UndeleteRole` |

### Google Workspace (`google_workspace_collector`)

**Actor ID:** `actor.profileId` (immutable numeric Google account ID; falls back to `actor.email`)  
**Config key:** `gws_application_names` (default: `["login", "admin", "token"]`)

| Application | Events captured |
|---|---|
| `login` | `login_success`, `login_failure`, `logout` |
| `admin` | `CREATE_USER`, `DELETE_USER`, `SUSPEND_USER`, `UNSUSPEND_USER`, `RENAME_USER`, `CREATE_GROUP`, `DELETE_GROUP`, `ADD_GROUP_MEMBER`, `REMOVE_GROUP_MEMBER` |
| `token` | `AUTHORIZE` (OAuth2 grant to a third-party app), `REVOKE` |

To poll a single application only: `"gws_application_names": ["login"]`

### Active Directory / Windows (`ad_collector`, `windows_eventlog_collector`)

**Actor ID:** `TargetSid` / `TargetUserSid` / `SubjectUserSid` (Windows Security Identifier — immutable; falls back to username)

Both collectors read exported `.evtx` files via `python-evtx`. `ad_collector` extends `windows_eventlog_collector` with Object Access events (file and share access) and is intended for Domain Controller Security logs.

| Event ID | Action | Notes |
|---|---|---|
| 4624 | Login (success) | All logon types |
| 4625 | Login (failure) | |
| 4634 | Logoff | |
| 4720 | CreateUser | |
| 4726 | DeleteUser | |
| 4732 | AddMemberToGroup | |
| 4738 | ChangeUserAccount | |
| 4663 | FileAccess | `ad_collector` only; always Success |
| 5140 | ShareAccess | `ad_collector` only; outcome from Keywords |

### Salesforce (`salesforce_collector`)

**Actor ID:** `USER_ID` (Salesforce internal user ID — immutable; falls back to `USER_NAME`)

Reads `LoginEvent` rows from Salesforce EventLogFile CSV exports. Captures login events only; extend `event_type` in `create_collector` to include other EventLogFile types (e.g. `ApexExecution`, `ReportExport`).

### Jaeger (`jaeger_collector`) · OpenTelemetry (`otel_collector`)

These collectors operate at the trace/span level rather than the identity-event level. Actor identity is whatever the instrumented service writes into span tags — no guaranteed immutable ID. Suitable for capturing service-to-service calls in environments where distributed traces are the authoritative activity record.

### JDBC (`jdbc_collector`) · Log file (`logfile_collector`)

Generic tabular collectors. Actor ID and all other fields are configured via `ColumnMap` / regex capture groups in the per-deployment instance — no default filter set.

### Acme Directory (`acme_directory_collector`)

Tutorial/reference collector against a fictional REST API. Not intended for production use.

---

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
| `DRY_RUN` | `false` | Set to `true` to print events to stdout instead of uploading — IGA credentials not required |
| `LIMIT` | _(none)_ | Stop each collector after N events; works with or without `DRY_RUN` |

CLI flags (`--dry-run`, `--limit N`) take precedence over env vars when both are set.

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
