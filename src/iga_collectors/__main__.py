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

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from iga_collectors.config import (
    ConfigError,
    build_collector_base_config,
    build_uploader,
    load_config,
)
from iga_collectors.discovery import discover_collector_files, load_collectors, run_all, run_and_upload
from iga_collectors.logging_setup import configure_logging
from iga_collectors.uploader import DryRunUploader

logger = logging.getLogger("iga_collectors")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="iga-collectors",
        description="Discover and run IGA activity collectors.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List discovered collectors in COLLECTORS_DIR and exit.",
    )
    parser.add_argument(
        "--collector",
        metavar="NAME",
        help="Run only the named collector from COLLECTORS_DIR.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Poll and map events but skip uploading — print to stdout instead. "
            "IGA credentials are not required. Checkpoint state is never read or written. "
            "Use with --limit to cap output."
        ),
    )
    parser.add_argument(
        "--limit",
        metavar="N",
        type=int,
        default=None,
        help="Stop each collector after N events. Usable with or without --dry-run.",
    )
    args = parser.parse_args()

    # Env var fallbacks — CLI flags take precedence when explicitly set.
    dry_run = args.dry_run or os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    limit = args.limit
    if limit is None and os.environ.get("LIMIT"):
        try:
            limit = int(os.environ["LIMIT"])
        except ValueError:
            pass

    if dry_run:
        configure_logging()
        collectors_dir_str = os.environ.get("COLLECTORS_DIR")
        if not collectors_dir_str:
            logger.error("COLLECTORS_DIR is not set")
            return 1
        collectors_dir = Path(collectors_dir_str)
        try:
            discovered = discover_collector_files(collectors_dir)
        except NotADirectoryError as exc:
            logger.error("%s", exc)
            return 1
        if not discovered:
            logger.warning("no collector files found in %s", collectors_dir)
            return 0

        print("--- DRY RUN: events printed to stdout, nothing uploaded ---", file=sys.stderr)
        if limit is not None:
            print(f"--- limit: {limit} event(s) per collector ---", file=sys.stderr)

        uploader = DryRunUploader()
        with tempfile.TemporaryDirectory() as tmpdir:
            base_config = {"checkpoint_path": str(Path(tmpdir) / "checkpoint.json")}
            if args.collector:
                stems = {p.stem for p in discovered}
                if args.collector not in stems:
                    logger.error(
                        "no collector named %r in %s — use --list to see available collectors",
                        args.collector, collectors_dir,
                    )
                    return 1
                collector_path = next(p for p in discovered if p.stem == args.collector)
                config_path = collector_path.with_suffix(".json")
                if config_path.exists():
                    try:
                        cfg = json.loads(config_path.read_text())
                        if not cfg.get("enabled", True):
                            logger.error(
                                "collector %r is disabled — set \"enabled\": true in %s to run it",
                                args.collector, config_path.name,
                            )
                            return 1
                    except (OSError, json.JSONDecodeError):
                        pass
                collectors, _, _ = load_collectors(collectors_dir, base_config)
                if args.collector not in collectors:
                    logger.error("collector %r was found but failed to load", args.collector)
                    return 1
                run_and_upload(collectors[args.collector], uploader, limit=limit)
            else:
                run_all(collectors_dir, base_config, uploader, limit=limit)
        return 0

    if args.list:
        collectors_dir_str = os.environ.get("COLLECTORS_DIR")
        if not collectors_dir_str:
            logger.error("COLLECTORS_DIR is not set")
            return 1
        try:
            files = discover_collector_files(Path(collectors_dir_str))
        except NotADirectoryError as exc:
            logger.error("%s", exc)
            return 1
        if not files:
            print("No collectors found in", collectors_dir_str)
        else:
            for p in files:
                label = p.stem
                config_path = p.with_suffix(".json")
                if config_path.exists():
                    try:
                        cfg = json.loads(config_path.read_text())
                        if not cfg.get("enabled", True):
                            label += "  [disabled]"
                        elif cfg.get("dry_run"):
                            label += "  [dry-run]"
                    except (OSError, json.JSONDecodeError):
                        pass
                print(label)
        return 0

    try:
        config = load_config()
    except ConfigError as exc:
        configure_logging()
        logger.error("configuration error: %s", exc)
        return 1

    configure_logging(config.log_level, config.log_format)

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

    if args.collector:
        stems = {p.stem for p in discovered}
        if args.collector not in stems:
            logger.error(
                "no collector named %r in %s — use --list to see available collectors",
                args.collector, config.collectors_dir,
            )
            return 1
        collector_path = next(p for p in discovered if p.stem == args.collector)
        config_path = collector_path.with_suffix(".json")
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text())
                if not cfg.get("enabled", True):
                    logger.error(
                        "collector %r is disabled — set \"enabled\": true in %s to run it",
                        args.collector, config_path.name,
                    )
                    return 1
            except (OSError, json.JSONDecodeError):
                pass
        collectors, _, _ = load_collectors(config.collectors_dir, base_config)
        if args.collector not in collectors:
            logger.error("collector %r was found but failed to load", args.collector)
            return 1
        t0 = time.monotonic()
        count = run_and_upload(collectors[args.collector], uploader, limit=limit)
        logger.info(
            "run_complete collectors_run=1 collectors_skipped=0 collectors_failed=0 "
            "events_uploaded=%d duration_s=%.1f",
            count, time.monotonic() - t0,
        )
        return 0

    t0 = time.monotonic()
    summary = run_all(config.collectors_dir, base_config, uploader, limit=limit)
    duration = time.monotonic() - t0

    total_events = sum(summary.results.values())
    logger.info(
        "run_complete collectors_run=%d collectors_skipped=%d collectors_failed=%d "
        "events_uploaded=%d duration_s=%.1f",
        len(summary.results), summary.skipped, len(summary.failed),
        total_events, duration,
    )

    if summary.failed:
        logger.warning(
            "collector(s) did not complete successfully: %s",
            ", ".join(sorted(summary.failed)),
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())