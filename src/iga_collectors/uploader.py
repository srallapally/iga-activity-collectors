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