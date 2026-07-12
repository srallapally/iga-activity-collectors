---
name: review-conventions
description: Use when reviewing code in the iga-collectors repo to apply project-specific standards for schema conformance, SDK/customer boundary, collector patterns, and known intentional stubs.
---

## Repo overview

`src/iga_collectors/` is a pip-installable SDK. `examples/` contains reference collector implementations that customers copy to their own `COLLECTORS_DIR` at runtime — they are never auto-discovered from this repo. `tests/` holds unit tests plus a synthetic `sample_collectors_dir/` fixture. Only `iga_collectors.base` and `iga_collectors.mapping` are listed as fully implemented; several modules and most example collectors are explicitly stubs.

---

## 1. What to always check

### Schema conformance

Every collector's output must be a dict conforming to `docs/activity_log_schema.json`. Check:

- **Required top-level fields** are all present: `id`, `schema_version`, `actor_global_id`, `event_id`, `event_time`, `event_type`, `action`, `outcome`. These are enforced by `new_event_shell()` in `base.py` — any path that bypasses `new_event_shell()` and constructs the dict manually must supply all eight.
- **`outcome`** must be one of `"success"`, `"failure"`, `"partial"`, `"unknown"`. `new_event_shell()` raises `ValueError` for anything else.
- **`event_type`** must be one of the eleven schema enum values: `authentication`, `resource_access`, `tool_invocation`, `knowledge_base_query`, `guardrail_evaluation`, `agent_invocation`, `token_issuance`, `policy_evaluation`, `secret_access`, `workload_attestation`, `authz_decision`.
- **`event_time`** must be a timezone-aware ISO 8601 datetime string (UTC-normalised). `new_event_shell()` rejects naive datetimes.
- **All nested object fields** (`actor`, `agent_context`, `auth_context`, `resource`, `access_context`, `correlation_context`, `environment`, `result_context`, `ingest_metadata`, `metadata`) use `additionalProperties: false` in the schema. Any key written into those objects that does not appear in the schema is invalid.
- **`schema_version`**: The SDK constant is `SCHEMA_VERSION = "3.0.0"` in `base.py`. The schema file's `"version"` field is `"4.0.0"`. This mismatch is a known open issue worth flagging if touched by a diff; do not treat it as an emergency in unrelated code.
- **Array fields** (e.g., `effective_roles`, `delegation_chain`) must be serialised as JSON-encoded strings in CSV output, not bare values. This is handled by `mapping._scalar_to_cell()`.

### SDK / customer-code boundary

- `src/iga_collectors/` is the SDK. It must have no dependency on any specific collector's source system (no `import boto3`, `import google.auth`, etc. in SDK files).
- `examples/` is customer reference code. Source-specific imports (`boto3`, `requests`, `confluent_kafka`, `google.auth`, `jaydebeapi`) belong exclusively here.
- The SDK's only runtime dependency is `requests>=2.31` (see `pyproject.toml`). Optional extras (`jdbc`, `kafka`, `google`, `aws`) exist purely for customer convenience and must not appear in `install_requires`.
- `examples/` files are never imported by the SDK. If a change introduces an `examples/` import into `src/`, flag it.

### Collector entry point contract

Every discoverable collector `.py` file must expose a module-level:

```python
def create_collector(config: dict[str, Any]) -> BaseCollector: ...
```

Files starting with `_` are intentionally skipped by discovery. The returned object must be a `BaseCollector` subclass instance; `discovery.load_collectors()` validates this and skips non-conforming files with a warning.

### Type hints

All SDK functions and methods carry full type hints. Check:
- Return types are annotated on all public methods.
- `Optional[X]` (or `X | None` in newer style) is used rather than bare `X` for nullable parameters.
- `dict[str, Any]` for untyped dicts; `list[dict[str, Any]]` for event batches.
- No bare `dict` or `list` without type parameters on public interfaces.

### `from __future__ import annotations`

Every module (SDK and examples) starts with `from __future__ import annotations` as the first non-docstring, non-comment line. New files must include it.

### Logging

Module-level `logger = logging.getLogger(__name__)` in every file that logs. No `print()` statements in SDK code.

---

## 2. Code style

### Module structure

```
# src/iga_collectors/module_name.py     <- comment with path, first line
"""
Module docstring — explains design intent and SailPoint concept mapping
where relevant.
"""

from __future__ import annotations

import stdlib_modules
from third_party import modules
from iga_collectors import internal_modules
```

### Docstrings

- Module-level: prose explaining the design intent and where this fits in the SailPoint analogy.
- Class-level: one sentence explaining the SailPoint equivalent and key design decision.
- Method-level: brief single-sentence or short block. Not Google/NumPy style — plain prose.
- Abstract methods: docstring explains the contract the subclass must fulfil.

### Naming conventions

- Public API: `snake_case` throughout.
- Private methods and attributes: `_` prefix.
- Module-level constants: `UPPER_SNAKE_CASE`.
- Inclusion-filter frozensets: always `frozenset`, never mutable.

### Constructor pattern for collectors

All collector constructors use **keyword-only arguments** (`*` separator) and forward unknown kwargs via `**base_kwargs` or `**declarative_kwargs` to `super().__init__()`. Example:

```python
def __init__(
    self,
    *,
    api_base_url: str,
    api_key: str,
    session: Optional[requests.Session] = None,
    timeout: int = 30,
    **declarative_kwargs: Any,
):
    super().__init__(**declarative_kwargs)
```

### Injectability / testability

All external I/O dependencies are injectable:
- HTTP-based collectors: `session: Optional[requests.Session] = None`; default is `requests.Session()`.
- boto3-based: `client_factory: Optional[Callable[..., Any]] = None`; default is the real boto3 factory.
- JDBC: `connect_fn` or equivalent.

A new collector that makes any network or I/O call without an injectable seam should be flagged.

### Checkpoint / first-run pattern

```python
if since_position is not None:
    since_dt = datetime.fromisoformat(since_position)
elif self._initial_lookback_seconds is not None:
    since_dt = now - timedelta(seconds=self._initial_lookback_seconds)
else:
    raise ValueError(
        "no checkpoint exists yet and initial_lookback_seconds is "
        "not configured; the first run needs an explicit starting point"
    )
```

Raise `ValueError`, never silently proceed with an undefined window.

---

## 3. Key invariants — never violate these

1. **`_native_actor_id` and `_event_time` are reserved field map keys.** They are consumed by `DeclarativeMappedCollector._record_to_activity()` and must not be written literally into the canonical event.

2. **`event_type` goes in the field map JSON, not the collector constructor.** `DeclarativeMappedCollector` reads it from `field_map["event_type"]`. Passing `event_type=` as a constructor kwarg raises `TypeError` at runtime.

3. **Naive datetimes are rejected, not silently interpreted.** `new_event_shell()` raises `ValueError` on a naive `event_time`.

4. **Outcome transforms must return `None` for "no evidence", not `"success"`.** The `default` key in the field map is the correct place to declare what "no data" means. A transform that returns `"success"` when the input is absent silently masks failed events as successful.

5. **`filter_by_checkpoint=False` is required for offset-based sources (Kafka/OTel).** Applying the timestamp-based checkpoint filter to a Kafka consumer silently drops valid data due to clock skew.

6. **`run()` must not be overridden.** Subclasses implement `poll()` / `poll_records()`, `next_position()`, and `map_to_event()`.

7. **`actor_global_id` must be a real IGA UUID in production.** `PassthroughCorrelator` returns the native ID unchanged and logs a warning. It is valid only in tests.

8. **CSV mapping doc keys must be dotted canonical schema paths.** A non-schema path produces a mapping document the upload API will reject.

9. **`create_collector(config)` must return a `BaseCollector` instance.** `discovery.load_collectors()` checks `isinstance(collector, BaseCollector)`.

10. **Per-collector JSON config files must be a flat JSON object**, not an array or scalar.

---

## 4. What NOT to flag

- **Stub modules and stub test files are intentionally incomplete.** Do not flag missing implementations as bugs. Explicitly stubbed items: `examples/unfinished/racf_audit_log_collector.py`, all four `tests/test_*.py` files.

- **`PassthroughCorrelator` in example collectors.** All examples use it because no real IGA store is available. Only flag if introduced into non-example production code paths.

- **`SCHEMA_VERSION = "3.0.0"` vs schema file `"version": "4.0.0"` mismatch.** Pre-existing open issue. Only flag if the diff directly touches either value.

- **Absence of the `actor` block in basic events.** The `actor` object is optional in the schema. A basic event without `actor` is valid.

- **`tests/fixtures/sample_collectors_dir/dummy_collector.py`** having no `create_collector` function — it's a discovery-test fixture for negative-case scanning.

---

## 5. Field map document checklist

When reviewing a `.fieldmap.json` file:

| Check | Correct form |
|---|---|
| Top-level keys | `source_system`, `event_type`, `fields` only |
| `event_type` value | One of the eleven schema enum values |
| `_native_actor_id` present | `"required": true` expected |
| `_event_time` present | `"required": true`, `transform` matches timestamp format of source |
| `outcome` field | Has `"default": "unknown"` (not `"success"`) unless the source guarantees an outcome for every record |
| `action` field | `"required": true` expected |
| Other field keys | Dotted path into `activity_log_schema.json`; verified against schema |
| `transform` values | Must be registered in `FIELD_TRANSFORMS` in `field_mapping.py`; unknown names raise `FieldMapError` at runtime |
| Fallback `source_path` lists | Valid syntax: `["path.one", "path.two"]`; first non-`None` wins |
| `$json` segment | Used when a sub-object arrives pre-serialised |

---

## 6. Test expectations for new collector code

New implemented (non-stub) collectors should include:

- At least one test using the `FakeResponse`/`FakeSession` (or equivalent client factory) pattern — no real network calls in tests.
- Assertions on `events[0]["action"]`, `events[0]["outcome"]`, `events[0]["actor_global_id"]` for a known fixture record.
- A test exercising cursor/token pagination (at least two "pages" in the fake).
- If the collector has an event-type or event-name inclusion filter, a test that confirms excluded records do not appear in output.
- No source-specific import at test module level — these must be imported lazily inside the default client/session factory only.
