# src/iga_collectors/config.py
"""
Loads runtime configuration from environment variables — see
.env.example at the project root for the full list and example values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from iga_collectors.uploader import ActivityUploader, TokenClient, build_upload_url

REQUIRED_VARS = [
    "IGA_PROTOCOL",
    "IGA_HOST",
    "IGA_PORT",
    "IGA_UPLOAD_PATH",
    "IGA_TOKEN_URL",
    "IGA_CLIENT_ID",
    "IGA_CLIENT_SECRET",
    "COLLECTORS_DIR",
    "CHECKPOINT_STORE_PATH",
]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    iga_protocol: str
    iga_host: str
    iga_port: int
    iga_upload_path: str
    iga_token_url: str
    iga_client_id: str
    iga_client_secret: str
    iga_oauth_scope: Optional[str]
    collectors_dir: Path
    checkpoint_store_path: Path
    log_level: str
    log_format: str

    @property
    def upload_url(self) -> str:
        return build_upload_url(
            self.iga_protocol, self.iga_host, self.iga_port, self.iga_upload_path
        )


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    """Read and validate config from environment variables. Raises
    ConfigError listing every missing required variable at once, rather
    than failing on the first one, so a customer fixes their .env in a
    single pass instead of one error at a time."""
    env = env if env is not None else os.environ

    missing = [name for name in REQUIRED_VARS if not env.get(name)]
    if missing:
        raise ConfigError(
            f"missing required environment variable(s): {', '.join(missing)}"
        )

    try:
        port = int(env["IGA_PORT"])
    except ValueError as exc:
        raise ConfigError(
            f"IGA_PORT must be an integer, got {env['IGA_PORT']!r}"
        ) from exc

    return Config(
        iga_protocol=env["IGA_PROTOCOL"],
        iga_host=env["IGA_HOST"],
        iga_port=port,
        iga_upload_path=env["IGA_UPLOAD_PATH"],
        iga_token_url=env["IGA_TOKEN_URL"],
        iga_client_id=env["IGA_CLIENT_ID"],
        iga_client_secret=env["IGA_CLIENT_SECRET"],
        iga_oauth_scope=env.get("IGA_OAUTH_SCOPE") or None,
        collectors_dir=Path(env["COLLECTORS_DIR"]),
        checkpoint_store_path=Path(env["CHECKPOINT_STORE_PATH"]),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
        log_format=env.get("LOG_FORMAT", "text").lower(),
    )


def build_uploader(config: Config) -> ActivityUploader:
    """Convenience: wire a Config into a ready-to-use ActivityUploader."""
    token_client = TokenClient(
        token_url=config.iga_token_url,
        client_id=config.iga_client_id,
        client_secret=config.iga_client_secret,
        scope=config.iga_oauth_scope,
    )
    return ActivityUploader(upload_url=config.upload_url, token_client=token_client)


def build_collector_base_config(config: Config) -> dict[str, Any]:
    """The base_config every collector's create_collector(config) receives
    before its own sibling {stem}.json (see discovery.py) is merged on
    top. Deliberately small: this Config is IGA-upload-side settings, not
    a place for per-collector credentials — checkpoint_path is the one
    thing every collector genuinely shares (they key into the same
    checkpoint file by their own collector_id; see base.CheckpointStore)."""
    return {"checkpoint_path": str(config.checkpoint_store_path)}