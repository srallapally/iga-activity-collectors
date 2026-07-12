# src/iga_collectors/__main__.py
"""
Entrypoint: discovers every collector in COLLECTORS_DIR, runs each one,
and uploads its output to IGA. Intended to be invoked by cron, a systemd
timer, or any other external scheduler — this process does one run and
exits; it is not a long-running daemon.

Invocation:
    python -m iga_collectors
or, once installed (pip install -e .):
    iga-collectors

Exit codes:
    0 - every discovered collector ran and uploaded successfully
    1 - fatal error before any collector could run (bad config, missing
        COLLECTORS_DIR, etc.)
    2 - ran, but at least one discovered collector failed (see logs for
        which); this is a partial-failure signal for monitoring, not
        necessarily an emergency — one broken collector shouldn't be read
        as "the whole run is broken."
"""

from __future__ import annotations

import logging
import sys

from iga_collectors.config import (
    ConfigError,
    build_collector_base_config,
    build_uploader,
    load_config,
)
from iga_collectors.discovery import discover_collector_files, run_all

logger = logging.getLogger("iga_collectors")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 1

    try:
        discovered = discover_collector_files(config.collectors_dir)
    except NotADirectoryError as exc:
        logger.error("%s", exc)
        return 1

    if not discovered:
        logger.warning("no collector files found in %s", config.collectors_dir)
        return 0

    uploader = build_uploader(config)
    base_config = build_collector_base_config(config)

    results = run_all(config.collectors_dir, base_config, uploader)

    total_events = sum(results.values())
    logger.info(
        "run complete: %d/%d collector(s) succeeded, %d event(s) uploaded",
        len(results), len(discovered), total_events,
    )
    for name, count in sorted(results.items()):
        logger.info("  %s: %d event(s)", name, count)

    if len(results) < len(discovered):
        failed = {p.stem for p in discovered} - set(results)
        logger.warning("collector(s) did not complete successfully: %s", ", ".join(sorted(failed)))
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())