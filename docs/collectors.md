# Collector Reference

Event coverage, actor ID fields, and configuration keys for every bundled
collector. All filter sets are configurable at runtime — set the
corresponding key in the per-collector `.json` config to a list to override
the defaults shown here.

---

## AWS CloudTrail (`aws_collector`)

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

---

## Azure ARM (`azure_collector`)

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

---

## Microsoft Entra ID (`entra_collector`)

**Actor ID:** `initiatedBy.user.id` / `initiatedBy.app.servicePrincipalId` / `initiatedBy.app.appId` (audit); `userId` (user sign-ins); `servicePrincipalId` (SP sign-ins) — all immutable AAD object IDs. Falls back to `"system"` for Microsoft-internal operations.

Runs three independent sub-streams, each with its own checkpoint:

| Stream | Checkpoint key | Coverage |
|---|---|---|
| Directory audits | `entra_directory_audits` | Account/group/app lifecycle changes — all audit log categories |
| User sign-ins | `entra_sign_ins` | Interactive and non-interactive user authentication |
| Service principal sign-ins | `entra_sp_sign_ins` | Daemon/app OAuth2 flows (`client_credentials`, certificate auth) — requires Azure AD Premium P1/P2; skipped with a warning if unavailable |

Required Graph API permissions (application, admin consent): `AuditLog.Read.All`, `Directory.Read.All`

---

## Microsoft 365 (`office365_collector`)

**Actor ID:** `UserId` (as logged by M365; may be a UPN or a synthetic app identity for workload events)  
**Config key:** `o365_operation_names` (default: no filter — all operations in the content type)  
**Config key:** `o365_content_type` (default: `Audit.Exchange`)

Available content types: `Audit.Exchange`, `Audit.SharePoint`, `Audit.General`, `Audit.AzureActiveDirectory`. Set `o365_content_type` in the per-collector `.json` to switch.

> **Note:** `Audit.AzureActiveDirectory` overlaps with the Entra collector — enable one or the other, not both.

---

## Okta System Log (`okta_collector`)

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

---

## GCP Cloud Audit Logs (`google_cloud_collector`)

**Actor ID:** `protoPayload.authenticationInfo.principalEmail` (service account email or user email — no immutable numeric ID available from this API)  
**Config key:** `gcp_method_names`

| Category | Methods captured |
|---|---|
| IAM policy | `SetIamPolicy` |
| Service account lifecycle | `CreateServiceAccount`, `DeleteServiceAccount`, `UndeleteServiceAccount`, `EnableServiceAccount`, `DisableServiceAccount`, `PatchServiceAccount`, `UpdateServiceAccount` |
| Service account keys | `CreateServiceAccountKey`, `DeleteServiceAccountKey`, `UploadServiceAccountKey` |
| Workload identity federation | `CreateWorkloadIdentityPool`, `UpdateWorkloadIdentityPool`, `DeleteWorkloadIdentityPool`, `UndeleteWorkloadIdentityPool`, `CreateWorkloadIdentityPoolProvider`, `UpdateWorkloadIdentityPoolProvider`, `DeleteWorkloadIdentityPoolProvider`, `UndeleteWorkloadIdentityPoolProvider` |
| Custom roles | `CreateRole`, `UpdateRole`, `DeleteRole`, `UndeleteRole` |

---

## Google Workspace (`google_workspace_collector`)

**Actor ID:** `actor.profileId` (immutable numeric Google account ID; falls back to `actor.email`)  
**Config key:** `gws_application_names` (default: `["login", "admin", "token"]`)

| Application | Events captured |
|---|---|
| `login` | `login_success`, `login_failure`, `logout` |
| `admin` | `CREATE_USER`, `DELETE_USER`, `SUSPEND_USER`, `UNSUSPEND_USER`, `RENAME_USER`, `CREATE_GROUP`, `DELETE_GROUP`, `ADD_GROUP_MEMBER`, `REMOVE_GROUP_MEMBER` |
| `token` | `AUTHORIZE` (OAuth2 grant to a third-party app), `REVOKE` |

To poll a single application only: `"gws_application_names": ["login"]`

---

## Active Directory / Windows (`ad_collector`, `windows_eventlog_collector`)

**Actor ID:** `TargetSid` / `TargetUserSid` / `SubjectUserSid` (Windows Security Identifier — immutable; falls back to username)

Both collectors read exported `.evtx` files via `python-evtx`. `ad_collector` extends the Windows collector with Object Access events (file and share access) and is intended for Domain Controller Security logs specifically.

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

---

## Salesforce (`salesforce_collector`)

**Actor ID:** `USER_ID` (Salesforce internal user ID — immutable; falls back to `USER_NAME`)

Reads `LoginEvent` rows from Salesforce EventLogFile CSV exports. Captures login events only by default; extend `event_type` in `create_collector` to include other EventLogFile types (e.g. `ApexExecution`, `ReportExport`).

---

## Jaeger (`jaeger_collector`) · OpenTelemetry (`otel_collector`)

These collectors operate at the trace/span level rather than the identity-event level. Actor identity is whatever the instrumented service writes into span tags — no guaranteed immutable ID. Suitable for capturing service-to-service calls in environments where distributed traces are the authoritative activity record.

---

## JDBC (`jdbc_collector`) · Log file (`logfile_collector`)

Generic tabular collectors. Actor ID and all other fields are configured via `ColumnMap` / regex capture groups in the per-deployment instance — no default filter set.

---

## Acme Directory (`acme_directory_collector`)

Tutorial/reference collector against a fictional REST API. See [DEVELOPER_COOKBOOK.md](../DEVELOPER_COOKBOOK.md) for how to use it as a template.
