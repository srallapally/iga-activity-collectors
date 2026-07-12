# src/iga_collectors/mapping.py
"""
Converts canonical ActivityLogEvent dicts (as produced by BaseCollector.run())
into the CSV + mapping doc pair required by:

    POST {{protocol}}://{{host}}:{{port}}/iga/governance/activity?_action=upload

The mapping doc mirrors the canonical schema's shape; each leaf is
{"column": "<csv header>"}. Per confirmed API behavior, the mapping does no
value transformation — every CSV cell must already be schema-valid:
  - scalars: their string form
  - booleans: "true" / "false"
  - null/missing: "" (empty cell)
  - arrays: JSON-encoded text (e.g. '["role1", "role2"]')

CSV column headers are the dotted canonical path (e.g. "actor.actor_type"),
matching the convention shown in the working example payload.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any


def flatten_event(event: dict[str, Any]) -> dict[str, str]:
    """Flatten one canonical event dict to {dotted_path: csv_cell_string}."""
    out: dict[str, str] = {}
    _flatten_into(event, "", out)
    return out


def _flatten_into(value: Any, prefix: str, out: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, sub_value in value.items():
            path = f"{prefix}.{key}" if prefix else key
            _flatten_into(sub_value, path, out)
        return

    if not prefix:
        raise ValueError(f"top-level event value must be an object, got {type(value)!r}")

    out[prefix] = _scalar_to_cell(value)


def _scalar_to_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        # v4 schema's only array fields are arrays of strings (e.g.
        # effective_roles, delegation_chain); JSON-encode as agreed.
        return json.dumps(value)
    if isinstance(value, (str, int, float)):
        return str(value)
    raise TypeError(f"cannot encode value of type {type(value)!r} for CSV: {value!r}")


def events_to_csv(events: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """
    Build CSV text for a batch of canonical events. Column set is the union
    of dotted paths populated across the batch (different collectors/event
    types populate different subsets of the schema); missing fields for a
    given event become empty cells.

    Returns (csv_text, sorted_columns).
    """
    if not events:
        raise ValueError("events must be non-empty")

    flattened = [flatten_event(e) for e in events]
    columns = sorted({path for row in flattened for path in row})

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, restval="", extrasaction="raise")
    writer.writeheader()
    writer.writerows(flattened)
    return buf.getvalue(), columns


def build_mapping_doc(columns: list[str]) -> dict[str, Any]:
    """Build the nested {"column": "<path>"} mapping doc for the given set
    of dotted canonical paths, matching the shape the upload API expects."""
    mapping: dict[str, Any] = {}
    for path in columns:
        _set_nested(mapping, path.split("."), path)
    return mapping


def _set_nested(node: dict[str, Any], parts: list[str], column: str) -> None:
    key = parts[0]
    if len(parts) == 1:
        node[key] = {"column": column}
        return
    child = node.setdefault(key, {})
    if "column" in child:
        raise ValueError(
            f"path conflict building mapping doc: {column!r} collides with "
            f"an existing leaf mapping at {key!r}"
        )
    _set_nested(child, parts[1:], column)


def build_upload_payload(events: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Convenience wrapper: events -> (csv_text, mapping_doc)."""
    csv_text, columns = events_to_csv(events)
    return csv_text, build_mapping_doc(columns)
