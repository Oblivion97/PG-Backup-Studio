# PG Backup Studio — Project Status

_Last updated: 2026-10-26_

Spec: `.claude/commands/pg-backup-studio.md` · Design: `docs/ARCHITECTURE.md` · Decisions:
`docs/decisions/` · Tests: `docs/QA_REPORT.md`

## ▶ Resume here (next action)
**Step 3 — Storage and jobs: the design is done and WAITING FOR USER APPROVAL.** Nothing is built.
Show the user the plan in `docs/ARCHITECTURE.md` §10 / `docs/decisions/005-step3-storage-jobs.md`
and the 6 decisions below. Build only after an explicit "approved".

How to start a new session (PowerShell, in this folder):
```
$env:PGPASSWORD = '<postgres password>'
claude --agent project-lead
```
The lead reads the spec + this file and continues from "Resume here". `claude --continue` reopens
the previous conversation instead.

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
| 3 | Storage and jobs | ⏸ Design done — **waiting for user approval** |
| 4 | Database management | ⏳ Not started |
| 5 | JSON API | ⏳ Not started |
| 6 | UI | ⏳ Not started |
| 7 | End-to-end + docs (README) | ⏳ Not started — then push to git |

## What works now (engine only, no UI or API yet)
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
- **Tests:** 380 passing against PG 18.4. The run takes about 14 min:
  `$env:PGPASSWORD='<pw>'; python -m pytest -q`

## Step 3 — waiting for approval (recommendations)
1. App closed during a job: cancel it cleanly (up to 15 s) and record "cancelled". After a hard
   kill, mark the job "interrupted" at the next start and delete its `.part` file. No resume.
2. Create the history entry at job start (status "running"). This changes the spec, which only
   records at the end, and it enables crash cleanup.
3. Restrict `data/` with `icacls` to the user + SYSTEM. This also blocks other admins' direct
   access. Alternative: default Windows permissions.
4. History capped at 1000 entries (configurable); trimming never deletes backup files.
5. At most 4 jobs at once; no two restores into the same DB.
6. The last successful output folder becomes the default for new DBs; each DB keeps its own.

Also in Step 3:
- fix the 3 minor Step 2 bugs (S2-3, S2-4, S2-5)
- fix the parallel-backup filename clash found by the architect: claim the `.part` file
  exclusively

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
- The target DB for a restore must be empty.
- Backup files must be UTF-8. A BOM is tolerated, and CRLF-converted files still restore.

## Environment (verified)
- Windows 11, Python 3.14.6, PostgreSQL 18.4 at localhost:5432, user `postgres`.
- Tests take the password from `PGPASSWORD`; it is never stored in any file.
- Test databases and roles are always named `pgbs_test_*`.

## Open questions
- Only PG 18 is installed, so PG 12–17 is covered only by tests with a patched server version.
  Real testing would need Docker or extra installs.
- The fixture role `pgbs_test_reader` is cluster-level. Restoring the fixture onto another server
  gives missing-role warnings, which is by design.
