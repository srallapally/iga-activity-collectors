# examples/aws_collector.py
"""
AWS Collector — CloudTrail, via boto3's lookup_events API.

Auth is a genuinely different model from every other cloud collector in
this project: AWS uses its own credential chain (access key/secret key,
IAM role, instance profile, environment variables), not OAuth2. No
TokenClient reuse applies here — boto3 handles credential resolution
itself. aws_access_key_id/aws_secret_access_key are optional constructor
params; if omitted, boto3's default credential chain is used (the
idiomatic choice when running on EC2/ECS/Lambda with an attached IAM
role, rather than static keys).

Client construction is behind an injectable client_factory (same
testability pattern as every other collector's connect_fn/
consumer_factory/token_provider) so tests never need boto3 installed.

Scope: CloudTrail's Management Events cover the entire AWS API surface —
overwhelmingly not account/identity-governance relevant (EC2 instance
launches, S3 bucket operations, etc.). event_names is configurable (same
pattern as operation_names in azure_collector.py / windows_eventlog_collector.py)
and defaults to a curated set of IAM/account-lifecycle events —
CreateUser, DeleteUser, access key and login profile changes, group
membership, ConsoleLogin, AssumeRole. Pass event_names=None to disable
filtering entirely.

Field mapping is now DECLARATIVE, driven by the JSON document at
aws_collector.fieldmap.json (loaded below) via
iga_collectors.field_mapping.DeclarativeMappedCollector, not Python code.
This class's only job is API mechanics: authenticating, calling
lookup_events, following NextToken pagination, and deciding which raw
CloudTrail events are even worth mapping (the event_names filter — that's
inclusion/exclusion of whole records, not a per-field mapping decision,
so it stays here rather than becoming part of the field map). poll_records()
yields raw CloudTrail event dicts completely unmodified; the field map
document (and the shared interpreter in field_mapping.py) does everything
from there — deciding that EventName becomes action, that a presence of
CloudTrailEvent.errorCode becomes outcome=failure, that
CloudTrailEvent.userIdentity.accessKeyId becomes both
auth_context.client_id and (via a transform) auth_context.credential_type.
See aws_collector.fieldmap.json directly to read that mapping without
reading Python at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

DEFAULT_EVENT_NAMES = frozenset({
    # IAM user lifecycle
    "CreateUser", "DeleteUser", "UpdateUser",
    "CreateLoginProfile", "DeleteLoginProfile", "UpdateLoginProfile",
    "CreateVirtualMFADevice", "DeactivateMFADevice", "EnableMFADevice",
    # IAM user credential management
    "CreateAccessKey", "DeleteAccessKey", "UpdateAccessKey",
    # IAM group membership
    "AddUserToGroup", "RemoveUserFromGroup",
    "CreateGroup", "DeleteGroup",
    # IAM policy attach/detach (users, roles, groups)
    "AttachUserPolicy", "DetachUserPolicy",
    "AttachRolePolicy", "DetachRolePolicy",
    "AttachGroupPolicy", "DetachGroupPolicy",
    "PutUserPolicy", "DeleteUserPolicy",
    "PutRolePolicy", "DeleteRolePolicy",
    # IAM role lifecycle
    "CreateRole", "DeleteRole", "UpdateRole", "UpdateAssumeRolePolicy",
    "CreateServiceLinkedRole", "DeleteServiceLinkedRole",
    # Authentication and session assumption
    "ConsoleLogin",
    "AssumeRole", "AssumeRoleWithWebIdentity", "AssumeRoleWithSAML",
    # Workload identity federation
    "CreateOpenIDConnectProvider", "DeleteOpenIDConnectProvider",
    "UpdateOpenIDConnectProviderThumbprint",
    "AddClientIDToOpenIDConnectProvider",
    "RemoveClientIDFromOpenIDConnectProvider",
    "CreateSAMLProvider", "DeleteSAMLProvider", "UpdateSAMLProvider",
})

FIELD_MAP_PATH = Path(__file__).parent / "aws_collector.fieldmap.json"


class AWSCloudTrailCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        region_name: str,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        event_names: Optional[frozenset[str]] = DEFAULT_EVENT_NAMES,
        initial_lookback_seconds: Optional[int] = None,
        max_results: int = 50,
        client_factory: Optional[Callable[..., Any]] = None,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._region_name = region_name
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._event_names = event_names
        self._initial_lookback_seconds = initial_lookback_seconds
        self._max_results = max_results
        self._client_factory = client_factory or _default_client_factory

    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        if since_position is not None:
            since_dt = datetime.fromisoformat(since_position)
        elif self._initial_lookback_seconds is not None:
            since_dt = now - timedelta(seconds=self._initial_lookback_seconds)
        else:
            raise ValueError(
                "no checkpoint exists yet and initial_lookback_seconds is "
                "not configured; the first run needs an explicit starting point"
            )

        client = self._client_factory(
            self._region_name, self._aws_access_key_id, self._aws_secret_access_key
        )
        call_kwargs: dict[str, Any] = {
            "StartTime": since_dt, "EndTime": now, "MaxResults": self._max_results,
        }
        while True:
            response = client.lookup_events(**call_kwargs)
            for event in response.get("Events", []):
                event_name = event.get("EventName")
                if not event_name:
                    continue
                if self._event_names is not None and event_name not in self._event_names:
                    continue
                yield event

            next_token = response.get("NextToken")
            if not next_token:
                break
            call_kwargs["NextToken"] = next_token


def _default_client_factory(region_name, access_key_id, secret_access_key):
    import boto3
    kwargs: dict[str, Any] = {"region_name": region_name}
    if access_key_id and secret_access_key:
        kwargs["aws_access_key_id"] = access_key_id
        kwargs["aws_secret_access_key"] = secret_access_key
    return boto3.client("cloudtrail", **kwargs)


# ---------------------------------------------------------------------------
# Reference example.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    event_names = config.get("aws_event_names")
    event_names = frozenset(event_names) if event_names else DEFAULT_EVENT_NAMES
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return AWSCloudTrailCollector(
        region_name=config["aws_region"],
        aws_access_key_id=config.get("aws_access_key_id"),
        aws_secret_access_key=config.get("aws_secret_access_key"),
        event_names=event_names,
        initial_lookback_seconds=config.get("aws_initial_lookback_seconds", 3600),
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="aws_cloudtrail_iam",
        source_system="aws_cloudtrail",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )