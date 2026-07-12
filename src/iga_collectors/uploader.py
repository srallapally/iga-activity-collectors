# src/iga_collectors/uploader.py
"""
OAuth2 client_credentials token acquisition, and multipart upload of a
batch of canonical events to:

    POST {{protocol}}://{{host}}:{{port}}/iga/governance/activity?_action=upload

Payload shape (confirmed against a working example):
    - form field "mapping": JSON text, the nested column-mapping doc
    - form field "file": CSV file, one row per event

Assumption (standard OAuth2 client_credentials; not yet verified against
the real IGA token endpoint): the token response is JSON with at least
"access_token" and "expires_in" fields.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import requests

from iga_collectors.mapping import build_upload_payload

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
TOKEN_EXPIRY_BUFFER_SECONDS = 30


def build_upload_url(protocol: str, host: str, port: int, path: str) -> str:
    path = path if path.startswith("/") else f"/{path}"
    return f"{protocol}://{host}:{port}{path}?_action=upload"


class TokenRequestError(Exception):
    pass


class UploadError(Exception):
    pass


class TokenClient:
    """Fetches and caches an OAuth2 client_credentials bearer token."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._session = session or requests.Session()
        self._timeout = timeout
        self._cached_token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        if self._cached_token is not None and time.monotonic() < self._expires_at:
            from datetime import datetime, timezone
            expires_iso = datetime.fromtimestamp(
                time.time() + (self._expires_at - time.monotonic()), tz=timezone.utc
            ).isoformat()
            logger.debug("token cache hit expires_at=%s", expires_iso)
            return self._cached_token
        return self._fetch_token()

    def _fetch_token(self) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scope:
            data["scope"] = self._scope

        logger.debug(
            "token request POST %s grant_type=client_credentials client_id=%s scope=%s",
            self._token_url, self._client_id, self._scope or "",
        )

        response = self._session.post(self._token_url, data=data, timeout=self._timeout)
        if not response.ok:
            logger.warning(
                "token request failed status=%s url=%s",
                response.status_code, self._token_url,
            )
            raise TokenRequestError(
                f"token request to {self._token_url} failed: "
                f"{response.status_code} {response.text}"
            )

        body = response.json()
        access_token = body.get("access_token")
        if not access_token:
            raise TokenRequestError(
                f"token response from {self._token_url} had no access_token field"
            )

        expires_in = body.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > TOKEN_EXPIRY_BUFFER_SECONDS:
            self._expires_at = time.monotonic() + expires_in - TOKEN_EXPIRY_BUFFER_SECONDS
            from datetime import datetime, timezone
            expires_iso = datetime.fromtimestamp(
                time.time() + expires_in - TOKEN_EXPIRY_BUFFER_SECONDS, tz=timezone.utc
            ).isoformat()
            logger.info("token refreshed expires_at=%s", expires_iso)
        else:
            # Unknown or too-short lifetime: don't cache, refetch every call.
            self._expires_at = 0.0
            logger.info("token refreshed (no caching: expires_in=%s)", expires_in)

        self._cached_token = access_token
        return access_token


class ActivityUploader:
    """Uploads a batch of canonical events to the IGA activity upload API."""

    def __init__(
        self,
        upload_url: str,
        token_client: TokenClient,
        session: Optional[requests.Session] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._upload_url = upload_url
        self._token_client = token_client
        self._session = session or requests.Session()
        self._timeout = timeout

    def upload(self, events: list[dict[str, Any]]) -> requests.Response:
        """Flatten events to CSV + mapping doc and POST them. Raises
        UploadError on a non-2xx response."""
        csv_text, mapping_doc = build_upload_payload(events)
        csv_bytes = len(csv_text.encode())

        logger.debug(
            "upload POST %s events=%d csv_bytes=%d",
            self._upload_url, len(events), csv_bytes,
        )

        token = self._token_client.get_token()
        response = self._session.post(
            self._upload_url,
            headers={"Authorization": f"Bearer {token}"},
            data={"mapping": json.dumps(mapping_doc)},
            files={"file": ("activity.csv", csv_text, "text/csv")},
            timeout=self._timeout,
        )
        if not response.ok:
            body_excerpt = response.text[:500]
            logger.error(
                "upload failed status=%s url=%s response=%r",
                response.status_code, self._upload_url, body_excerpt,
            )
            raise UploadError(
                f"upload to {self._upload_url} failed: "
                f"{response.status_code} {response.text}"
            )

        logger.info(
            "upload accepted status=%s events=%d", response.status_code, len(events)
        )
        return response


def check_connectivity(
    token_client: TokenClient,
    upload_url: str,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Probe IGA token endpoint and upload endpoint reachability.

    Returns a dict with keys:
      token_ok (bool), token_expires_at (str|None), token_error (str|None)
      endpoint_ok (bool), endpoint_status (int|None), endpoint_error (str|None)

    Never raises — all errors are captured in the return dict so the caller
    can print a structured report and choose the exit code."""
    result: dict = {
        "token_ok": False, "token_expires_at": None, "token_error": None,
        "endpoint_ok": False, "endpoint_status": None, "endpoint_error": None,
    }
    session = session or requests.Session()

    try:
        token = token_client.get_token()
        result["token_ok"] = True
        # Approximate expiry from cached state — best-effort display only.
        import time as _time
        from datetime import datetime, timezone as _tz
        remaining = token_client._expires_at - _time.monotonic()
        if remaining > 0:
            expires_iso = datetime.fromtimestamp(
                _time.time() + remaining, tz=_tz.utc
            ).isoformat()
            result["token_expires_at"] = expires_iso
    except Exception as exc:
        result["token_error"] = str(exc)
        return result

    # Strip query string from upload_url for the probe — we just want to
    # reach the base path, not trigger an actual upload action.
    probe_url = upload_url.split("?")[0]
    try:
        response = session.head(
            probe_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            allow_redirects=True,
        )
        result["endpoint_status"] = response.status_code
        # Any response — including 405 Method Not Allowed — means the
        # server is reachable and the token was accepted for routing.
        # 401/403 means the token was rejected by the server.
        if response.status_code in (401, 403):
            result["endpoint_error"] = (
                f"HTTP {response.status_code} — token was acquired but rejected "
                f"by the upload endpoint; check IGA_CLIENT_ID / IGA_CLIENT_SECRET scopes"
            )
        else:
            result["endpoint_ok"] = True
    except requests.exceptions.ConnectionError as exc:
        result["endpoint_error"] = f"connection failed — {exc}"
    except requests.exceptions.Timeout:
        result["endpoint_error"] = f"timed out after {timeout}s"
    except Exception as exc:
        result["endpoint_error"] = str(exc)

    return result


class DryRunUploader:
    """Drop-in replacement for ActivityUploader used by --dry-run.
    Prints each event as pretty-printed JSON to stdout. Makes no HTTP
    calls — no IGA credentials required."""

    def upload(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            print(json.dumps(event, indent=2))