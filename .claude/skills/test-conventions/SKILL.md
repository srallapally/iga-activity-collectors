---
name: test-conventions
description: Reference for how tests are structured, run, and extended in the iga-collectors repo — consult before writing or modifying any test.
---

## How to run the test suite

Install the package in editable mode with dev dependencies, then run pytest from the project root:

```bash
pip install -e ".[dev]"
pytest
```

There is no custom pytest configuration in `pyproject.toml` (no `[tool.pytest.ini_options]` section), so all pytest defaults apply. Running `pytest` from the repo root is sufficient — pytest discovers tests automatically via its standard `tests/` directory convention.

## Test file conventions

- All tests live in `tests/` at the project root (not inside `src/`).
- One test file per source module, named `test_<module>.py`:

| Test file | Source module |
|---|---|
| `tests/test_base.py` | `src/iga_collectors/base.py` |
| `tests/test_mapping.py` | `src/iga_collectors/mapping.py` |
| `tests/test_discovery.py` | `src/iga_collectors/discovery.py` |
| `tests/test_uploader.py` | `src/iga_collectors/uploader.py` |

- The source modules `config.py`, `field_mapping.py`, and `main.py` do not yet have corresponding test files.
- There is no `conftest.py` at any level; no shared fixtures or pytest plugins are wired up yet.

## Fixtures

`tests/fixtures/` contains two items:

### `tests/fixtures/testActivity.csv`
A single-row CSV file with the canonical activity event schema:

```
event_id,event_time,event_type,action,outcome,actor_global_id
evt-sample-1,2026-07-10T00:00:00+00:00,authentication,Login,success,00000000-0000-0000-0000-000000000001
```

Intended for use in tests that exercise CSV ingestion, field mapping, or schema validation logic.

### `tests/fixtures/sample_collectors_dir/dummy_collector.py`
A minimal fake external collector module (stub, no real implementation). Its purpose is to give `test_discovery.py` a populated directory to scan when testing `iga_collectors.discovery`'s dynamic collector-discovery logic. The file itself is explicitly documented as "not a real source integration."

There is no `conftest.py` fixture loader; fixture files are meant to be opened directly by test code via their filesystem paths (e.g., `pathlib.Path(__file__).parent / "fixtures" / "testActivity.csv"`).

## Framework

- **pytest >= 8.0** (declared as a `dev` optional dependency in `pyproject.toml`).
- No pytest plugins are declared (no `pytest-cov`, `pytest-mock`, `pytest-asyncio`, etc.).
- No `[tool.pytest.ini_options]` section exists in `pyproject.toml` — all pytest configuration is at defaults.
- No `conftest.py` exists anywhere in the project.

The only runtime dependency of the package itself is `requests >= 2.31`. Optional extras (`jdbc`, `kafka`, `google`, `aws`) exist for reference collector integrations but are not installed in the dev environment unless explicitly requested.

## Current status

All four test files are stubs. Each contains only a module-level docstring and zero test functions:

```python
# tests/test_base.py
"""STUB — placeholder test module for base."""
```

This pattern is repeated verbatim in `test_mapping.py`, `test_discovery.py`, and `test_uploader.py`. The test suite currently passes trivially (0 tests collected). All substantive test authoring is pending.

When adding tests, follow the one-file-per-module convention, load fixture files via `pathlib.Path(__file__).parent / "fixtures" / ...`, and add shared fixtures to a new `tests/conftest.py` if more than one test module needs them.
