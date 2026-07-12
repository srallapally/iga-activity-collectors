# examples/jdbc_collector.py
"""
JDBC Collector — JDBC activity data source (reads activity rows from a database table/view via SQL).

Uses JayDeBeApi (https://pypi.org/project/JayDeBeApi/) as the JDBC bridge.
This has a real operational dependency beyond `pip install`: JayDeBeApi
wraps the driver through JPype, so it needs a JVM installed on the host
plus the vendor's JDBC driver .jar file(s) — there is no pure-Python JDBC
client. Confirm `java -version` works and you have the driver jar before
deploying this collector.

Field mapping is declarative — see jdbc_collector.fieldmap.json, loaded
below via iga_collectors.field_mapping.DeclarativeMappedCollector. This
class's only job is API mechanics: opening the JDBC connection, building
the "since last position" SQL condition (SailPoint's
ActivityConditionBuilder rule equivalent), and yielding raw SQL rows —
each already naturally a flat {column_name: value} dict via
dict(zip(columns, row)), needing no restructuring before the field map
can resolve paths against it directly.

The create_collector() example below reproduces the exact scenario from
SailPoint's own documentation: a MySQL `activity_example` table with
columns (time, user, action, target) — no result column, so outcome is a
literal "unknown" in the field map rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import timezone
from typing import Any, Callable, Iterator, Optional, Union

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

FIELD_MAP_PATH = Path(__file__).parent / "jdbc_collector.fieldmap.json"


class JDBCCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        driver_class: str,
        jdbc_url: str,
        jdbc_driver_jars: Union[str, list[str]],
        credentials: Optional[tuple[str, str]],
        sql_query: str,
        since_clause_builder: Callable[[Optional[str]], str],
        connect_fn: Optional[Callable[..., Any]] = None,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._driver_class = driver_class
        self._jdbc_url = jdbc_url
        self._jdbc_driver_jars = jdbc_driver_jars
        self._credentials = credentials
        self._sql_query = sql_query
        self._since_clause_builder = since_clause_builder
        self._connect_fn = connect_fn or _default_connect_fn

    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        since_clause = self._since_clause_builder(since_position)
        query = self._sql_query.format(since_clause=since_clause)

        conn = self._connect_fn(
            self._driver_class,
            self._jdbc_url,
            list(self._credentials) if self._credentials else [],
            self._jdbc_driver_jars,
        )
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(query)
                columns = [d[0] for d in cursor.description]
                for row in cursor.fetchall():
                    yield dict(zip(columns, row))
            finally:
                cursor.close()
        finally:
            conn.close()


def _default_connect_fn(driver_class, jdbc_url, credentials, jars):
    import jaydebeapi
    return jaydebeapi.connect(driver_class, jdbc_url, credentials, jars)


# ---------------------------------------------------------------------------
# Reference example: SailPoint documentation's own MySQL scenario.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    def since_clause_builder(since: Optional[str]) -> str:
        if since is None:
            return ""
        mysql_ts = since.replace("T", " ").split("+")[0].split(".")[0]
        return f"AND time > '{mysql_ts}'"

    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return JDBCCollector(
        driver_class="com.mysql.cj.jdbc.Driver",
        jdbc_url=config["jdbc_url"],
        jdbc_driver_jars=config["jdbc_driver_jars"],
        credentials=(config["db_user"], config["db_password"]),
        sql_query=(
            "SELECT time, user, action, target FROM activity_example "
            "WHERE 1=1 {since_clause} ORDER BY time"
        ),
        since_clause_builder=since_clause_builder,
        field_map=field_map,
        source_timezone=timezone.utc,
        collector_id="jdbc_activity_example",
        source_system="mysql_activity_example",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )