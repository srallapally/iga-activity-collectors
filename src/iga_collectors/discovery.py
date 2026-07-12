# src/iga_collectors/discovery.py
"""
Scans COLLECTORS_DIR (external, not part of this repo) for collector
modules and runs them, uploading their output via ActivityUploader.

Discovery contract: each discoverable .py file must define a module-level

    def create_collector(config: dict) -> BaseCollector: ...

Files starting with "_" are skipped (lets customers keep shared helper
modules alongside real collectors in the same directory). A file that
defines no create_collector, or whose create_collector raises, is skipped
with a logged warning rather than aborting discovery of the rest — one
broken customer collector shouldn't block the others, same as SailPoint
running each activity data source as a separate aggregation task.

Per-collector configuration: each collector needs its own credential bag
(an Entra tenant/client/secret, AWS keys, an Okta API token, ...) that has
nothing to do with any other collector's, and nothing to do with the
IGA-upload-side Config in config.py. Convention: a collector at
COLLECTORS_DIR/foo_collector.py may have a sibling
COLLECTORS_DIR/foo_collector.json holding exactly its own settings as a
flat JSON object. That file is optional — a collector needing no extra
config (e.g. one reading purely from environment variables itself) simply
has none. What every collector actually receives is base_config (shared
values every collector needs, e.g. checkpoint_path — see config.py's
build_collector_base_config) with that collector's own JSON file's keys
merged on top, so per-collector keys always win over shared ones.

Security note, not enforced by this code: per-collector JSON files sitting
in COLLECTORS_DIR will contain plaintext credentials. Lock down file
permissions on that directory at the OS level; this module does not check
or restrict them.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable

from iga_collectors.base import BaseCollector
from iga_collectors.uploader import ActivityUploader

logger = logging.getLogger(__name__)

ENTRY_POINT_NAME = "create_collector"
DEFAULT_BATCH_SIZE = 100


class CollectorLoadError(Exception):
    """One collector file failed to import or lacked a valid entry point."""


def discover_collector_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise NotADirectoryError(
            f"COLLECTORS_DIR does not exist or is not a directory: {directory}"
        )
    return sorted(p for p in directory.glob("*.py") if not p.name.startswith("_"))


def _collector_config_path(collector_file: Path) -> Path:
    return collector_file.with_suffix(".json")


def _load_collector_config(
    collector_file: Path, base_config: dict[str, Any]
) -> dict[str, Any]:
    """base_config with collector_file's sibling {stem}.json merged on top,
    if that file exists. Raises CollectorLoadError on malformed JSON —
    treated as a per-file failure like any other, not a hard stop."""
    config = dict(base_config)
    config_path = _collector_config_path(collector_file)
    if not config_path.exists():
        logger.info(
            "no config file %s for %s; using base config only",
            config_path.name, collector_file.name,
        )
        return config

    try:
        per_collector = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorLoadError(f"failed to load {config_path}: {exc}") from exc

    if not isinstance(per_collector, dict):
        raise CollectorLoadError(
            f"{config_path} must contain a JSON object, got {type(per_collector).__name__}"
        )

    config.update(per_collector)
    return config


def _import_module(path: Path) -> ModuleType:
    module_name = f"iga_collectors._discovered.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise CollectorLoadError(f"could not build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[module_name]
        raise CollectorLoadError(f"failed to import {path}: {exc}") from exc
    return module


def load_collector_factory(path: Path) -> Callable[[dict[str, Any]], BaseCollector]:
    """Import one file and return its create_collector function. Raises
    CollectorLoadError if the file doesn't define one."""
    module = _import_module(path)
    factory = getattr(module, ENTRY_POINT_NAME, None)
    if factory is None:
        raise CollectorLoadError(
            f"{path} does not define {ENTRY_POINT_NAME}(config) -> BaseCollector"
        )
    if not callable(factory):
        raise CollectorLoadError(f"{path}.{ENTRY_POINT_NAME} is not callable")
    return factory


def load_collectors(
    directory: Path, base_config: dict[str, Any]
) -> dict[str, BaseCollector]:
    """
    Import every collector file in directory and instantiate it via its
    create_collector(config), where config is base_config merged with
    that collector's own sibling {stem}.json (see _load_collector_config).
    Returns {file_stem: BaseCollector instance}, only for files that
    loaded and instantiated successfully; failures are logged and skipped.
    """
    collectors: dict[str, BaseCollector] = {}
    for path in discover_collector_files(directory):
        try:
            config = _load_collector_config(path, base_config)
            factory = load_collector_factory(path)
            collector = factory(config)
        except CollectorLoadError as exc:
            logger.warning("skipping %s: %s", path, exc)
            continue
        except Exception as exc:
            logger.warning("skipping %s: create_collector(config) raised: %s", path, exc)
            continue

        if not isinstance(collector, BaseCollector):
            logger.warning(
                "skipping %s: create_collector(config) returned %r, not a BaseCollector",
                path, type(collector),
            )
            continue

        collectors[path.stem] = collector

    return collectors


def _batched(
    items: Iterable[dict[str, Any]], size: int
) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def run_and_upload(
    collector: BaseCollector,
    uploader: ActivityUploader,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Run one collector to completion, uploading its events in batches.
    Returns the number of events uploaded. Propagates errors from the
    collector or the uploader; run_all isolates those per collector."""
    uploaded = 0
    for batch in _batched(collector.run(), batch_size):
        uploader.upload(batch)
        uploaded += len(batch)
    return uploaded


def run_all(
    directory: Path,
    base_config: dict[str, Any],
    uploader: ActivityUploader,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """
    Discover and run every collector in directory. Each collector's
    create_collector(config) receives base_config merged with its own
    sibling {stem}.json, if one exists (see _load_collector_config). Each
    collector's failure is isolated: one collector erroring doesn't stop
    the others. Returns {collector_name: events_uploaded} for collectors
    that ran without error; failed collectors are logged and omitted from
    the result.
    """
    results: dict[str, int] = {}
    for name, collector in load_collectors(directory, base_config).items():
        try:
            results[name] = run_and_upload(collector, uploader, batch_size)
        except Exception:
            logger.exception("collector %s failed during run/upload", name)
    return results