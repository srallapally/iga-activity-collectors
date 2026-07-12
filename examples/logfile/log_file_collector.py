# examples/log_file_collector.py
"""
Log File Collector — Log File activity data source (extracts activity records from a log file using a
regular expression).

Log File data source configuration:
  - Regular Expression + separate "list of fields in order" tab
        -> a single Python regex with named groups (?P<field>...).
  - Lines to Skip           -> lines_to_skip
  - Filter Nulls            -> filter_nulls (skip non-matching lines
                                 instead of raising)
  - Multi-lined Data        -> multi_lined_data (match across the whole
                                 file with DOTALL instead of line-by-line)
  - Transport Settings      -> NOT implemented for FTP/SCP; local files
                                 only. Flagged, not silently dropped.

Field mapping is declarative — see log_file_collector.fieldmap.json,
loaded below via iga_collectors.field_mapping.DeclarativeMappedCollector.
This class's only job is API mechanics: reading the file, applying the
regex, skipping lines. Each regex match's named groups (m.groupdict())
are already a flat dict, needing no restructuring before the field map
can resolve paths against it directly.

Checkpoint filtering (records at or before the last checkpointed
event_time) is now handled centrally by DeclarativeMappedCollector.poll()
— this class no longer implements it itself, unlike the earlier
TabularActivityCollector-based version.

The create_collector() example below is illustrative: a simple space-separated line format
`<timestamp> <user> <action> <target> <result>`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

FIELD_MAP_PATH = Path(__file__).parent / "log_file_collector.fieldmap.json"


class LogFileCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        file_path: str,
        pattern: str,
        lines_to_skip: int = 0,
        filter_nulls: bool = False,
        multi_lined_data: bool = False,
        encoding: str = "utf-8",
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._file_path = Path(file_path)
        flags = re.DOTALL if multi_lined_data else 0
        self._pattern = re.compile(pattern, flags)
        self._lines_to_skip = lines_to_skip
        self._filter_nulls = filter_nulls
        self._multi_lined_data = multi_lined_data
        self._encoding = encoding

        if not self._pattern.groupindex:
            raise ValueError(
                "pattern must use named groups, e.g. (?P<action>\\S+), "
                "so matches can be mapped by the field map document"
            )

    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        text = self._file_path.read_text(encoding=self._encoding)
        lines = text.splitlines()
        if self._lines_to_skip:
            lines = lines[self._lines_to_skip:]
        yield from self._iter_matches(lines)

    def _iter_matches(self, lines: list[str]) -> Iterator[dict[str, str]]:
        if self._multi_lined_data:
            content = "\n".join(lines)
            for m in self._pattern.finditer(content):
                yield m.groupdict()
            return

        for line in lines:
            if not line:
                continue
            m = self._pattern.match(line)
            if m is None:
                if self._filter_nulls:
                    continue
                raise ValueError(f"line did not match pattern: {line!r}")
            yield m.groupdict()


# ---------------------------------------------------------------------------
# Illustrative example: a simple space-separated log line format.
#   2026-07-10T10:44:47+00:00 alice Login LDAP Success
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    pattern = (
        r"(?P<time>\S+)\s+(?P<user>\S+)\s+(?P<action>\S+)\s+"
        r"(?P<target>\S+)\s+(?P<result>\S+)"
    )
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return LogFileCollector(
        file_path=config["log_file_path"],
        pattern=pattern,
        filter_nulls=True,
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="log_file_example",
        source_system="example_app_log",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )