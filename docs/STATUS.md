# PG Backup Studio — Project Status

_Last updated: 2026-10-27_

Spec: `.claude/commands/pg-backup-studio.md` · Design: `docs/ARCHITECTURE.md` · Decisions:
`docs/decisions/` · Tests: `docs/QA_REPORT.md`

## ▶ Resume here (next action)
**Step 6 — UI: BUILT, waiting for the click-through.** `static/index.html` (about 139 KB, no libraries) and
`tests/test_step6_ui.py` (11 static checks, passing). Next: the user runs `python app.py`, opens
http://127.0.0.1:8000 and goes through ARCHITECTURE §13.7 items 4–12 (the Chrome extension was not connected,
so Claude could not click through it). Then mark Step 6 done and start Step 7.

How to start a new session (PowerShell, in this folder):
```
$env:PGPASSWORD = '<postgres password>'
claude --agent project-lead
```
The lead reads the spec + this file and continues from "Resume here". `claude --continue` reopens
the previous conversation instead.

## Overall progress (estimate, 2026-10-27)
**About 77% done by effort** (6 of 8 steps finished = 75% by step count).

| # | Step | Weight | Status |
|---|---|---|---|
| 0 | Project setup | 5% | ✅ Done |
| 1 | Backup + restore engine | 30% | ✅ Done |
| 2 | Pre-check, safety, partial backups | 15% | ✅ Done |
| 3 | Storage and jobs | 10% | ✅ Done |
| 4 | Database management | 7% | ✅ Done |
| 5 | JSON API | 10% | ✅ Done |
| 6 | UI | 15% | 🔨 Built, click-through pending |
| 7 | End-to-end + README | 8% | ⏳ |

The engine (Steps 1–2) was the hardest and riskiest part. The remaining steps are standard app
work, but the app can't be used from a browser until Step 6. Update this table when each step
closes.

## Decisions from the user
- Code lives directly in this folder (no `pgbackup/` subfolder).
- Python 3.14, local PostgreSQL 18 for testing.
- No git yet; it gets pushed to git once the app works.
- This file tracks done / left / open questions.

## Build progress

| # | Step | Status |
|---|---|---|
| 0 | Project setup | ✅ Done — QA PASS 64/64 (2026-10-25) |
| 1 | Backup + restore engine | ✅ Done — QA PASS 216 passed after 1 fix round (2026-10-25) |
| 2 | Pre-check, safety, partial backups | ✅ Done — QA PASS 380 passed, 3 xfail minor (2026-10-26) |
| 3 | Storage and jobs | ✅ Done — full suite 443 passed, 1 skipped (POSIX-only) (2026-10-27) |
| 4 | Database management | ✅ Done — full suite 460 passed + 1 fix re-run 42/42 (2026-10-27) |
| 5 | JSON API | ✅ Done — full suite 471 passed + flaky Step 4 test replaced, re-run 18/18 (2026-10-27) |
| 6 | UI | 🔨 Built (approved 2026-10-27); static checks 11/11; click-through pending |
| 7 | End-to-end + docs (README) | ⏳ Not started — then push to git |

## What works now (engine, storage, jobs, database management, JSON API; no UI yet)
- **Backup** (`engine/backup.py`):
  - reads every object from the catalogs (no pg_dump) and writes one `.sql` / `.sql.gz` file
  - modes: full, schema-only and data-only; schema filter; owners/grants on/off
  - one read-only snapshot, with up-front ACCESS SHARE locks under `lock_timeout`
  - speed limit
  - per-table Full / Latest N / Schema only, giving a `_partial` file name, a header block and
    FKs written as NOT VALID
  - `GRANT ALL` when the full privilege set is held
  - progress callback and cancel (works even during lock waits)
- **Pre-check** (`engine/precheck.py`):
  - permission problems (SELECT/USAGE/RLS) that block the start
  - table sizes, large flags, ordering-column suggestions, index advice, impact estimate,
    current locks
- **Restore** (`engine/restore.py`):
  - Python splitter/executor (no psql); plain or gzip (detected by magic bytes)
  - continue / stop / single-transaction modes
  - missing roles become warnings; progress and cancel
- **Storage** (`store.py`): servers, settings, history, backup profiles in `data/*.json`; atomic
  replace, one lock per folder, corrupt files moved aside, `version` field, `data/` restricted to
  the user + SYSTEM with `icacls`, history capped (default 1000).
- **Jobs** (`jobs.py`): background backup/restore threads, percent/ETA/200-line log snapshots,
  cancel, max 4 running, no two restores into one DB, history written at start and end, profile +
  default output folder saved after a successful backup, app close cancels jobs, `interrupted`
  after a hard kill.
- **Database management** (`engine/db.py`): list databases / roles / templates, sessions count,
  emptiness check, create, drop (typed name), rename, clone, kill connections; protected templates and
  maintenance DB; 63-byte names; retry when sessions reconnect. Restore targets existing / create /
  recreate in `jobs.py` (phase "preparing"); `jobs_using()` for the API.
- **JSON API** (`app.py`): every endpoint of ARCHITECTURE §12.2; Host + `X-PGBS` header guard against
  other websites; passwords never returned; unstored passwords asked once and kept in memory; one
  `{error, code}` format; open folder only for files recorded in history. `python app.py` serves it.
- **Tests:** 472 passing against PG 18.4 in about 16 min. In cmd: `set PGPASSWORD=<pw>` then
  `python -m pytest -q` (PowerShell: `$env:PGPASSWORD='<pw>'; python -m pytest -q`).

## Step history

### Step 0 — Project setup (QA PASS 64/64)
- `requirements.txt` (Flask 3.1.3, psycopg 3.3.6 binary, pytest 9.1.1), `.gitignore`,
  `app.py` (127.0.0.1:8000, PORT override), `static/index.html`, module stubs.
- `tests/conftest.py` and `tests/fixtures/sample_db.sql`, which covers every acceptance-test object.

### Step 1 — Backup + restore engine (QA PASS)
- Approved decisions:
  1. missing-role grants → warnings
  2. uncovered object kinds → "NOT INCLUDED" warning
  3. data-only restore → triggers off during the load
  4. extensions always included
  5. restore into an existing empty DB only, until Step 4
- QA round 1 FAIL with 4 bugs:
  - newline in a name broke the restore
  - raw zlib error on a damaged gzip
  - BOM not handled
  - empty schema list "succeeded"
- All four were fixed; the re-run passed.

### Step 2 — Pre-check, safety, partial backups (QA PASS)
- Approved decisions:
  1. lock tables at the start like pg_dump; this fixes a Step 1 gap where a concurrent TRUNCATE
     could silently save 0 rows
  2. narrower FK NOT VALID rule
  3. no "ignore" in the pre-check
  4. MAINTAIN → `GRANT ALL`
  5. profile file saving deferred to Step 3
- A sequence-privilege bug (with more than about 50 sequences) was found and fixed.
- About 130 QA tests: read-only users, no writes, lock timeout, 7 kinds of DDL against the
  up-front lock, cancel, FK matrix, index advice checked against EXPLAIN, concurrent DDL,
  too-many-locks.

### Step 3 — Storage and jobs (PASS, 2026-10-27)
- Approved decisions (all six recommendations):
  1. app closed during a job → cancel (15 s grace); hard kill → `interrupted` at next start and
     its recorded `.part` deleted; no resume
  2. history entry created at job start (`running`)
  3. `data/` restricted with `icacls` to the user + SYSTEM
  4. history capped at 1000 (configurable 50–100000); trimming never deletes files
  5. max 4 jobs at once; no two restores into the same DB
  6. last successful output folder becomes the default; each DB keeps its own profile
- Engine changes: `.part` reserved with exclusive create (parallel backups in the same second get
  distinct names); an explicit file name that already exists is now an error, not an overwrite;
  new `file` progress event; `bytes_written` on `object` / `table_done`.
- Fixed S2-3 (pre-check: 4500 tables 5.6 s → 0.58 s; batched index checks), S2-4 (dropped schema
  retried), S2-5 (non-dict settings).
- New tests: `tests/test_step3_store.py`, `tests/test_step3_jobs.py` (fake engine + real PG).

### Step 4 — Database management (PASS, 2026-10-27)
- Approved decisions (all six recommendations): protect template0/template1/maintenance DB from drop
  and rename; refuse actions on a DB a running job uses; restore target created inside the job;
  non-empty existing target refused unless allowed; kill without superuser reports `failed`; create
  uses server defaults, restore "new" uses template0 + UTF8.
- QA found 1 bug: the maintenance-DB fallback never triggered for `localhost` (psycopg drops the
  SQLSTATE when several addresses fail). Fixed by also matching the server message; re-run 42/42.
- New tests: `tests/test_step4_dbmgmt.py` (18).

### Step 5 — JSON API (PASS, 2026-10-27)
- Approved decisions (all seven recommendations): Host + `X-PGBS: 1` + JSON-only guard; unstored
  passwords via `/connect`, kept in memory only; `{error, code}` with fixed status codes; slow calls
  inside the request (only backup/restore are jobs); open folder only for history files; DB names with
  `/` not usable in URL actions; failed connection test is `{ok: false}`, not an error.
- Full suite: 471 passed, 1 failed, 1 skipped. The failure was a timing-dependent Step 4 test (a
  reconnecting session raced the rename). Replaced by a deterministic test that keeps the session
  open and checks the 3 retries and `in_use`; `tests/test_step4_dbmgmt.py` re-run 18/18.
- New tests: `tests/test_step5_api.py` (11; every response is scanned for the stored password).

## Known limitations (to go in the README)
- Not reproduced:
  - the original grantor
  - column storage/compression and statistics targets
  - typed tables
  - grants on extension-owned objects
- Separately attached partitions come back in the parent's column order (the data is correct).
- Range types with a canonical function only get a "may not restore" warning.
- Owner ACLs with MAINTAIN that aren't the full set stay an explicit list, which fails on
  PG 12–16 targets.
- Restoring as a non-superuser gives `OWNER TO` errors (the data still restores). Use a superuser,
  or back up without owners/grants.
- Choosing only some schemas can give restore errors when triggers, rules or FKs reference
  unselected schemas (the backup warns).
- Tables with only column-level grants can't be locked up front, so the TRUNCATE race remains for
  them.
- A schema-only table is still locked at the start; a lock error may name a child partition.
- The default ordering column for a composite PK is the first column by position.
- Restoring into a non-empty database is refused unless allowed (then "already exists" errors are expected).
- Run only one app instance per `data/` folder (two processes could overwrite each other's change).
- Backup files must be UTF-8. A BOM is tolerated, and CRLF-converted files still restore.
- Database names containing `/` are listed and can be backed up / restored, but drop / rename / kill /
  profile need a name without `/`.

## Environment (verified)
- Windows 11, Python 3.14.6, PostgreSQL 18.4 at localhost:5432, user `postgres`.
- Tests take the password from `PGPASSWORD`; it is never stored in any file.
- Test databases and roles are always named `pgbs_test_*`.

## Open questions
- Only PG 18 is installed, so PG 12–17 is covered only by tests with a patched server version.
  Real testing would need Docker or extra installs.
- The fixture role `pgbs_test_reader` is cluster-level. Restoring the fixture onto another server
  gives missing-role warnings, which is by design.
