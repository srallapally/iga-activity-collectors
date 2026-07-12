# src/iga_collectors/logging_setup.py
"""
Configures the root logger for the iga_collectors namespace.

Two formats are supported:
  text  (default) — human-readable, same as stdlib basicConfig
  json            — one JSON object per log record, for log aggregators
                    (ELK, Datadog, CloudWatch Logs Insights, etc.)

JSON record shape:
  {
    "time":      "2026-07-12T10:18:09.123Z",
    "level":     "INFO",
    "logger":    "iga_collectors.discovery",
    "msg":       "skipping okta_collector.py: disabled in config",
    // any extra fields passed via logging.xxx(..., extra={...})
  }

Extra fields (e.g. collector="okta_collector", event_count=42) are
included as top-level JSON keys when present. Standard LogRecord
attributes that would clutter the output (pathname, lineno, thread, …)
are excluded from the JSON form.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

_SKIP_ATTRS = frozenset({
    "args", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message",
    "module", "msecs", "msg", "name", "pathname", "process",
    "processName", "relativeCreated", "stack_info", "thread", "threadName",
})

_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)

        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"

        obj: dict = {
            "time": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.message,
        }

        if record.exc_text:
            obj["exc"] = record.exc_text

        for key, value in record.__dict__.items():
            if key not in _SKIP_ATTRS and not key.startswith("_"):
                obj[key] = value

        return json.dumps(obj, default=str)


def configure_logging(
    level: str = "INFO",
    fmt: str = "text",
    stream: Optional[object] = None,
) -> None:
    """Call once at process startup before any other log output.

    level: DEBUG | INFO | WARNING | ERROR (case-insensitive)
    fmt:   text | json
    stream: override output stream (default: sys.stderr); exposed for tests
    """
    level_upper = level.upper()
    if level_upper not in _VALID_LEVELS:
        level_upper = "INFO"

    root = logging.getLogger("iga_collectors")
    root.setLevel(level_upper)

    if root.handlers:
        root.handlers.clear()

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setLevel(level_upper)

    if fmt.lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )

    root.addHandler(handler)
