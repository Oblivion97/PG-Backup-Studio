# 001 — Project layout and Python/driver baseline

_Date: 2026-10-25 · Status: accepted (Step 0 approved by user)_

## Context
The spec's "Project structure" places code directly in the project folder. Dev machine: Windows 11,
Python 3.14.6, PostgreSQL 18.4 at `C:\Program Files\PostgreSQL\18`. Git comes later.

## Decision
1. **Flat root layout**: `app.py`, `jobs.py`, `store.py`, `engine/` (package with `__init__.py`),
   `static/index.html`, `tests/` (+ `tests/fixtures/`), `docs/`, `requirements.txt`, `.gitignore`.
   No `pgbackup/` package, no `setup.py`/`pyproject.toml`; run with `python app.py` from the root.
   Tests import modules by running pytest from the root (`tests/conftest.py` inserts the project root
   into `sys.path` so `import engine.util` works regardless of pytest rootdir handling).
2. **Dependencies** (`requirements.txt`): `Flask>=3.1`, `psycopg[binary]>=3.2.10`, `pytest>=8.4`.
   Nothing else. pytest is listed (not a separate dev file) to keep one install command.
3. **psycopg on Python 3.14 — verified 2026-10-25**: `pip index versions psycopg-binary` (filters to
   wheels compatible with this interpreter) lists 3.2.10 … 3.3.6; `pip download psycopg-binary
   --only-binary=:all:` fetched `psycopg_binary-3.3.6-cp314-cp314-win_amd64.whl`. So `psycopg[binary]`
   installs from a wheel, no compiler or system libpq needed. Minimum 3.2.10 = oldest release with a
   cp314 wheel.
4. **Fallback** (only if the binary wheel ever fails to install/import): `pip install psycopg` (pure
   Python) and put `C:\Program Files\PostgreSQL\18\bin` on `PATH` so it finds `libpq.dll`. Document in
   README; do not change code for it (`psycopg.pq.__impl__` reports `binary` or `python`).
5. **Runtime data** in `./data/` (created on first use, git-ignored). Backups default to `~/pg_backups`.

## Consequences
- Module names `app`, `jobs`, `store`, `engine` are top-level; avoid names shadowing stdlib.
- The engine must not import `app`/`jobs`/`store` (keeps it unit-testable).
- If packaging is wanted later, a `pyproject.toml` can be added without moving files.
