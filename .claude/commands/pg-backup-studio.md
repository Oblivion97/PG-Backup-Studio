---
description: Build PG Backup Studio — a local Python web app for catalog-based PostgreSQL backup, restore and database management
argument-hint: [optional extra instructions or a part to focus on, e.g. "only the restore engine"]
---

# PG Backup Studio — application specification

You are building **PG Backup Studio**, a Python web app that runs on the user's own PC, connects to
PostgreSQL servers, and backs databases up into a local folder **without using pg_dump**. It reads every
object (tables, functions, views, types, everything) one by one from the system catalogs, rebuilds the
SQL for each, and writes a single restorable `.sql` file. It can also restore those files into any
connected server and manage databases.

Extra instructions from the user for this run: $ARGUMENTS

## Working rules (follow strictly)

1. **Plan first.** Before writing any code, present a short implementation plan (what you will build,
   in what order, and any open questions) and **wait for the user's explicit approval**. Answering your
   questions is not approval — only build after the user clearly says to go ahead.
2. If the user interrupts or says stop, stop immediately and summarize what was done.
3. Build in the order given under "Build order", and test each part before moving on.
4. Keep everything local: the web server binds to `127.0.0.1` only.
5. This file is the **single source of truth**. When the user changes a requirement, update this file
   first, then the code.

## Development team (agents)

The project is built by a team of Claude Code agents in `.claude/agents/`:

| Agent | Role | May write |
|---|---|---|
| `project-lead` (mother agent) | Runs the whole process: plans with the user, gets approval, delegates, tracks status | `docs/STATUS.md`, this spec |
| `architect` | Designs each feature before coding; reviews large changes and design-level bugs | `docs/ARCHITECTURE.md`, `docs/decisions/*.md` |
| `senior-developer` | Implements one approved task at a time and self-tests against real PostgreSQL | Application code, `requirements.txt`, `README.md` |
| `qa-tester` | Verifies every change with real PostgreSQL tests; final say on "done" | `tests/`, `docs/QA_REPORT.md` |

**How to start:** in PowerShell in the project folder, set the test DB password
(`$env:PGPASSWORD = '...'`, never stored in a file), then run `claude --agent project-lead`. The
lead reads this file and `docs/STATUS.md` (section "Resume here"), then continues from the next
open step. (Running `/pg-backup-studio` in a normal
session also works; in that case the main session follows the same loop and delegates to the same
agents.)

**Development loop for every feature:**

1. **Understand** — the lead restates the feature and checks it against this spec.
2. **Design** — `architect` produces the design: files, interfaces, data flow, PostgreSQL details per
   version, risks, test ideas.
3. **Plan & approve** — the lead shows the user a short plan and **waits for explicit approval**.
4. **Build** — `senior-developer` implements exactly the approved scope and reports how it self-tested.
5. **Verify** — `qa-tester` runs real tests against the acceptance criteria and returns PASS / FAIL with
   a bug list.
6. **Fix loop** — failures go back to `senior-developer`, then to `qa-tester` again; design problems go
   to `architect`. After 3 failed rounds the lead reports the blocker to the user.
7. **Close** — the lead updates `docs/STATUS.md` and tells the user what works, how it was verified, and
   what is next.

A step of the build order is only finished when `qa-tester` returns **PASS** for it.

**Every delegation must include:** the goal and exact scope (in / out), the relevant section names of
this spec, the files involved and their current state, the acceptance criteria, and what to return.
Subagents start with no memory of the conversation, so never assume they know earlier context.

### Project documents

| File | Owner | Content |
|---|---|---|
| `.claude/commands/pg-backup-studio.md` | project-lead | This specification |
| `docs/STATUS.md` | project-lead | Done / in progress / next / known issues / open questions |
| `docs/ARCHITECTURE.md` | architect | Current design of every module and the API contract |
| `docs/decisions/NNN-title.md` | architect | Short records of important design decisions |
| `docs/QA_REPORT.md` | qa-tester | Dated test runs: verdict, cases, bugs, rerun commands |
| `tests/` and `tests/fixtures/` | qa-tester | Automated tests and the rich sample-DB SQL |
| `README.md` | senior-developer | Install, run, read-only user setup, restore, limitations |

## Tech stack

| Part | Choice |
|---|---|
| Web framework | Flask (threaded dev server is fine) |
| UI | One single-page `static/index.html` with plain JavaScript, no build step |
| DB driver | psycopg 3 (`psycopg[binary]`) |
| Storage | JSON files in `./data/` next to the app |
| Start | `python app.py` → `http://127.0.0.1:8000` (port overridable with `PORT` env var) |
| Supported servers | PostgreSQL **12 to latest (18)**; refuse older versions with a clear message |

## Project structure

The code lives directly in the project folder (`PG Backup Studio/`), not in a subfolder.
Development environment: Windows 11, Python 3.14, local PostgreSQL 18
(`C:\Program Files\PostgreSQL\18`). Tests connect using the standard libpq env vars
(`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`; default `localhost:5432`, user `postgres`).
Git is set up later, after the app works.

```
PG Backup Studio/
  app.py            # Flask routes / JSON API, serves static/index.html
  jobs.py           # background job runner + progress tracking
  store.py          # JSON files in ./data: servers.json, settings.json,
                    # history.json, backup_profiles.json
  engine/
    util.py         # lit(), stmt(), topo_sort(), Cancelled
    db.py           # connect, server info, DB list, create/drop/rename/clone, kill connections
    precheck.py     # permission check, table sizes, large-table flags, index check for "latest N"
    catalog.py      # catalog queries for every object type
    backup.py       # rebuilds DDL + COPY data -> one .sql / .sql.gz file
    restore.py      # SQL file splitter + executor with progress
  static/index.html
  requirements.txt
  README.md
  docs/             # STATUS.md, ARCHITECTURE.md, decisions/, QA_REPORT.md
  tests/            # automated tests + fixtures/ (sample-DB SQL)
  .claude/
    commands/pg-backup-studio.md   # this specification
    agents/                        # project-lead, architect, senior-developer, qa-tester
  data/             # created at runtime; add to .gitignore
```

## Feature 1 — Saved servers ("memory")

- Add / edit / delete servers: name, host, port, user, password, sslmode, optional maintenance DB.
- "Test connection" button before saving; shows server version and whether the user is superuser.
- Saved in `data/servers.json` and kept across restarts. **Passwords are stored as plain text** in that
  file (user approved this for now); set file permissions to 0600 where the OS allows.
- "Remember password" checkbox; when off, ask for the password when connecting.
- Sidebar lists saved servers; clicking one connects and opens its database list.
- The API must never send stored passwords back to the browser (return `has_password` instead).

## Feature 2 — Database list and management

After connecting, show all databases (excluding templates) with: name, size, owner, encoding,
active connection count. Actions:

| Action | Behaviour |
|---|---|
| Create | name, optional owner (pick from role list), template, encoding |
| Drop | user must type the DB name to confirm; terminate connections first; use `WITH (FORCE)` on PG 13+ |
| Rename | terminate connections first, `ALTER DATABASE ... RENAME TO` |
| Clone | `CREATE DATABASE new TEMPLATE src` (terminate connections to src first) |
| Kill connections | `pg_terminate_backend` for every other session on that DB |
| Backup | opens the backup panel for that DB |
| Restore | opens the restore panel with that DB as target |

Connect via a maintenance DB: the configured one, else `postgres`, else `template1`.
Quote all identifiers with `psycopg.sql.Identifier`.

## Feature 3 — Backup (the core)

User picks a DB from the list, sees the pre-backup check (below), then chooses:
- schemas to include (list loaded from the DB, all ticked by default)
- mode: **full** / schema-only / data-only
- include owners & grants (default on)
- gzip compression (default off)
- output folder (default `~/pg_backups`, remembered in `data/settings.json`)
- per-table data options for large tables, plus safety options (see below)

All of these are remembered per database (see "Remember the choices").

Output: one file `dbname_YYYYMMDD_HHMMSS.sql` (or `.sql.gz`). Write to `*.part` and rename on
success; delete the part file on failure or cancel.

### Read-only backup user
The backup must work with a **read-only login**; it never writes to the source database.
Required: `CONNECT` on the DB, `USAGE` on each schema, `SELECT` on all tables and sequences
(on PG 14+ the `pg_read_all_data` role covers this). Catalog definitions need no extra rights.
Tables with row-level security need `BYPASSRLS` or table ownership — because the session uses
`row_security = off`, the backup fails with a clear error instead of silently saving filtered rows.
The README shows how to create such a user for PG 12/13 and for PG 14+.

### Pre-backup check (runs when the user opens the backup panel for a DB)
Show before starting:
- **Permission check**: list every selected table/sequence the user cannot `SELECT`
  (`has_table_privilege`, `has_sequence_privilege`), schemas without `USAGE`, and RLS tables the user
  cannot bypass (`relrowsecurity` and not owner and not `rolbypassrls`). Block "Start" while any of
  these exist (or let the user exclude those schemas/tables).
- **Impact estimate**: total data size, total estimated rows, and the largest tables.
- A note that the backup is read-only but adds load, and a tip to run off-peak or against a read replica.

### Safety options (backup panel, "Advanced")
- **Lock timeout** (default 10 s): `SET lock_timeout`; if a table is locked by DDL the job fails with a
  clear message instead of hanging.
- **Speed limit** (optional, MB/s): throttle the COPY stream by sleeping between chunks.
- Never take locks beyond what reading needs; never run `ANALYZE`, `VACUUM` or any write.

### Large tables and partial backups
- In the pre-backup check, list every table with size (`pg_table_size`) and estimated rows (`reltuples`).
  Flag tables as **large** when over **500 MB or 1,000,000 estimated rows** (thresholds editable in
  settings).
- For every table (large ones highlighted) the user picks a data option:
  - **Full** (default)
  - **Latest N rows** — choose the ordering column (suggest PK columns and date/timestamp columns,
    default the first timestamp-like column, else the PK) and N (default 100,000). Export with
    `COPY (SELECT <cols> FROM <table> ORDER BY <col> DESC NULLS LAST LIMIT N) TO STDOUT`.
  - **Schema only** (no data)
- Warn when the ordering column has no index whose first column is that column (a "latest N" query
  would then sort the whole table).
- Partitioned tables are offered as one logical table (the parent), not per partition. For "latest N",
  select from the parent and write `COPY parent (...) FROM stdin` so rows route to the right partitions
  on restore; for "full", keep copying each leaf partition. Partitions of a parent that has a
  non-full option are not offered separately.
- **Remember the choices** per server + database in `data/backup_profiles.json` (keyed by server id
  and DB name): selected schemas, mode, privileges, compression, output folder, lock timeout, speed
  limit, and the per-table options. The next backup of the same DB opens with them pre-filled and a
  "Saved settings from <date>" note; a "Reset to defaults" button clears them. Ignore saved options for
  tables that no longer exist, and treat new tables as Full.
- When any table is partial or schema-only:
  - Write a foreign key as `NOT VALID` (append ` NOT VALID` to `pg_get_constraintdef()` unless
    already present) when the table it **references** is partial or schema-only and its own table
    has data in the backup, so restore succeeds. On PG 12–17 a FK on a partitioned table cannot be
    NOT VALID: leave it out, warn, and put the exact `ALTER TABLE ... ADD CONSTRAINT` in the header
    (see `docs/decisions/004-step2-locking-partial-fks.md`).
  - Add `_partial` to the file name and a header block listing each partial table, its option,
    ordering column and N.
  - Mark the backup as **Partial** in history and in the job result.
  - The README explains how to validate the FKs later: `ALTER TABLE ... VALIDATE CONSTRAINT ...`.

### Snapshot and session
- One connection, `REPEATABLE READ READ ONLY` transaction for the whole run (consistent snapshot).
- Like pg_dump, take `ACCESS SHARE` locks on every table being backed up at the start (under
  `lock_timeout`), so a concurrent TRUNCATE/rewrite cannot silently empty a table in the backup.
- When an owner/role holds every privilege on an object, emit `GRANT ALL` instead of the explicit
  list, so a restore onto an older server (e.g. without `MAINTAIN`) still works.
- The pre-check never offers "ignore": blocked schemas/tables must be excluded or set to
  "Schema only".
- `SET search_path = pg_catalog` so `pg_get_*def()` and `format_type()` return schema-qualified names.
- `SET client_encoding = 'UTF8'`, `statement_timeout = 0`.
- Exclude system schemas (`pg_*`, `information_schema`) and **every object owned by an extension**
  (`pg_depend.deptype = 'e'`) — `CREATE EXTENSION` recreates them.

### File header
Comment block (database, server version, host:port, timestamp, mode, schemas, privileges), then:
```
SET statement_timeout = 0; SET lock_timeout = 0; SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8'; SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false; SET xmloption = content;
SET client_min_messages = warning; SET row_security = off;
```
Precede every object with `-- Type: <KIND>; Name: <name>` (the restore progress uses these labels).

### Object order in the file
1. Schemas — `CREATE SCHEMA IF NOT EXISTS`
2. Extensions — `CREATE EXTENSION IF NOT EXISTS ... WITH SCHEMA ...` (skip plpgsql)
3. Types — enums, then domains (checks as `ALTER DOMAIN ... ADD CONSTRAINT`), ranges
   (SUBTYPE, SUBTYPE_DIFF, MULTIRANGE_TYPE_NAME on 14+), composites (only `relkind = 'c'`)
4. Sequences — non-identity ones, from `pg_sequence`
5. "Early" functions/procedures — `pg_get_functiondef()`
6. Tables — built by hand from `pg_attribute` (see rules below)
7. `ALTER SEQUENCE ... OWNED BY`
8. Table data — `COPY ... FROM stdin;` blocks, ordered by FK dependencies
9. Sequence values — `SELECT pg_catalog.setval(...)` for all sequences incl. identity ones
10. PK / unique / exclusion / check constraints — `pg_get_constraintdef()`
11. Indexes — `pg_get_indexdef()`, plus `CLUSTER ON`
12. Foreign keys
13. "Late" objects, topologically sorted together: views, materialized views (`WITH DATA`, or
    `WITH NO DATA` in schema-only mode), aggregates, SQL-standard-body functions, and functions that
    depend on a table/view row type
14. Indexes on materialized views
15. Triggers (+ ENABLE/DISABLE state), rules, row-level security flags, policies
16. After each object: comments, `OWNER TO`, and grants (when enabled)

### Table DDL rules
- Columns: name, `format_type`, `COLLATE` when it differs from the type default, `DEFAULT`,
  `GENERATED ALWAYS AS (...) STORED` (and `VIRTUAL` on PG 18), `NOT NULL`.
- Identity columns: `GENERATED ALWAYS|BY DEFAULT AS IDENTITY (SEQUENCE NAME <seq> START WITH ...
  INCREMENT BY ... MINVALUE ... MAXVALUE ... CACHE ... [NO] CYCLE)` so the sequence name survives.
- `UNLOGGED`, `WITH (reloptions)`, `PARTITION BY pg_get_partkeydef()`.
- Partitions: `CREATE TABLE child PARTITION OF parent <pg_get_expr(relpartbound)>`.
- Inheritance: only local columns (`attislocal`) plus `INHERITS (...)`.
- Create tables in topological order of `pg_inherits`.
- Column comments via `COMMENT ON COLUMN`.

### Data rules
- `COPY schema.table (cols) TO STDOUT`, streamed chunk by chunk straight into the file.
- Exclude generated columns from the column list.
- Copy only `relkind = 'r'` (leaf partitions and plain tables, never the partitioned parent).
- Count rows by counting newlines in the text COPY stream.
- For "latest N" tables, use N (or `reltuples` if smaller) as the row estimate and skip "schema only"
  tables in the data progress totals.

### Privileges
- Build grants with `aclexplode()`. When an ACL is not NULL, emit `REVOKE ALL ... FROM PUBLIC`,
  `REVOKE ALL ... FROM <owner>`, then one `GRANT` per entry (with `WITH GRANT OPTION` when set).
- Use the right object keyword: TABLE (also for views and materialized views), SEQUENCE,
  FUNCTION/PROCEDURE, SCHEMA, TYPE/DOMAIN.

### Version-specific gotchas (already verified — do not regress)
- Exclude functions with an internal dependency (`pg_depend.deptype = 'i'`): range types
  auto-create constructor and multirange functions.
- `prosqlbody` exists only on PG 14+; `tgparentid` only on PG 13+ (skip cloned partition triggers);
  `rngmultitypid` only on PG 14+. Gate those queries on `server_version_num`.
- Skip constraints with `conislocal = false` or `conparentid <> 0` (inherited / partition clones).
- Skip indexes that are partitions of a parent index (`relispartition`) and indexes that back a
  PK/unique/exclusion constraint. For indexes on partitioned tables, replace `ON ONLY` with `ON`
  so they recurse to partitions.
- Strip the trailing `;` from `pg_get_viewdef()` before appending `WITH DATA`.
- Aggregates: build `CREATE AGGREGATE` from `pg_aggregate` (SFUNC, STYPE, FINALFUNC, COMBINEFUNC,
  SERIALFUNC, DESERIALFUNC, INITCOND, PARALLEL). Ordered-set aggregates are skipped with a log note.
- In catalog queries passed to psycopg with params, literal `%` must be written as `%%`.

## Feature 4 — Restore

1. Pick a backup from history, or type a file path on the PC (`.sql` or `.sql.gz`, detected by
   gzip magic bytes).
2. Pick a connected server (it can be a different server from the source).
3. Target DB: an existing DB, a new name (created from `template0`, UTF8, optional owner), or
   "drop and recreate if exists".
4. Options: stop on first error, or continue and log errors (default); optional single transaction
   (all or nothing — implies stop on error).

Execute the file through psycopg — **no psql dependency**. The splitter must handle:
- single quotes (with `''`), `E'...'` strings with backslash escapes, double-quoted identifiers
- dollar quoting with tags (`$$`, `$function$`), but not `$1` parameters
- `--` line comments and nested `/* */` block comments
- SQL-standard bodies: inside `CREATE [OR REPLACE] FUNCTION|PROCEDURE`, count `BEGIN`/`CASE`
  as +1 and `END` as -1 and do not split on `;` while the depth is above 0
- `COPY ... FROM stdin;` followed by data lines up to `\.` — stream them with `cursor.copy()` in
  ~1 MB batches; on error skip the rest of that block

Track the `-- Type: ...; Name: ...` labels so progress shows the current object. Collect errors
(object, message, first 160 chars of the SQL) and show them in the result.

## Feature 5 — Progress (backup and restore)

Jobs run in background threads; the UI polls `GET /api/jobs/<id>` about every 700 ms.

Show for **backup**:
- overall progress bar with % and ETA (weight 20% objects, 80% data; data share from
  `pg_table_size` of tables done plus the bytes copied of the current table)
- objects done / total, tables done / total, rows copied vs. estimated rows (`reltuples`),
  current table with its own row count, bytes written to the file so far, elapsed time

Show for **restore**:
- % by compressed file position / file size, current object, rows loaded, error count, elapsed, ETA

For both: a phase name, a live log (last 200 lines, errors highlighted), a **Cancel** button
(checked between objects and between COPY chunks), and a jobs list so several jobs can run at once.

## Feature 6 — History

- `data/history.json` records each backup and restore: id, kind, server, database, file, size,
  started, duration, status, object counts, row count, error count, error message, and for backups
  a `partial` flag with the list of partial / schema-only tables (shown as a "Partial" badge).
- Per entry: Restore (pre-fills the restore panel), Open folder (opens the file manager on the PC
  via `os.startfile` / `open` / `xdg-open`), Delete entry (optionally also delete the file, with
  confirmation).

## JSON API (suggested)

```
GET    /api/servers                    POST /api/servers            (create/update)
DELETE /api/servers/<id>               POST /api/servers/test        (unsaved form data)
GET    /api/servers/<id>/info
GET    /api/servers/<id>/databases     GET  /api/servers/<id>/roles
POST   /api/servers/<id>/databases     (create / clone)
DELETE /api/servers/<id>/databases/<name>
POST   /api/servers/<id>/databases/<name>/rename
POST   /api/servers/<id>/databases/<name>/kill
GET    /api/servers/<id>/databases/<name>/schemas
POST   /api/servers/<id>/databases/<name>/check      (permission check + table sizes; body: schemas)
GET    /api/servers/<id>/databases/<name>/profile    (saved backup options)
DELETE /api/servers/<id>/databases/<name>/profile    (reset to defaults)
POST   /api/backup                     POST /api/restore
GET    /api/jobs                       GET  /api/jobs/<id>     POST /api/jobs/<id>/cancel
GET    /api/history                    DELETE /api/history/<id>
POST   /api/history/<id>/open-folder
GET    /api/settings                   POST /api/settings
```
Return `{"error": "..."}` with a 4xx/5xx status on failure; show it in the UI as a toast.

## UI layout

- Left sidebar: app name, saved servers (status dot when connected), "+ Add server".
- Main area tabs: **Databases** · **Backup** · **Restore** · **Activity** (jobs + progress) · **History**.
- Modals for add/edit server, create/clone/rename/drop DB (drop needs the name typed to confirm).
- Clean, modern look, light and dark mode via `prefers-color-scheme`, works at laptop widths.
- Human-readable sizes (KB/MB/GB) and durations.

## Build order

Each step runs through the full development loop (design → user approval → build → QA PASS).

| # | Step | Architect designs | Developer builds | QA verifies |
|---|---|---|---|---|
| 0 | Project setup | Folder layout, `docs/ARCHITECTURE.md` skeleton | Folders, `requirements.txt`, empty modules | Sample-DB fixture in `tests/fixtures/`, test runner, local PostgreSQL setup |
| 1 | Backup + restore engine | Catalog queries per object type and version, ordering, splitter | `util.py`, `db.py`, `catalog.py`, `backup.py`, `restore.py` | Full round-trip acceptance test |
| 2 | Pre-check, safety, partial backups | Permission check, size flags, "latest N", FK `NOT VALID`, profiles | `precheck.py`, lock timeout, speed limit, partial options in `backup.py` | Read-only user, no-writes, lock timeout, partial-backup tests |
| 3 | Storage and jobs | JSON file formats, job/progress model, cancel | `store.py`, `jobs.py`, profile save/load (moved from step 2) | Progress 0→100, cancel cleanup, files survive restart, next backup pre-filled from profile |
| 4 | Database management | Safe create/drop/rename/clone/kill | Functions in `db.py` | Each action incl. active connections and bad names |
| 5 | JSON API | Endpoint contract, error format | `app.py` | Every endpoint success + error; passwords never returned |
| 6 | UI | Screens, flows, states | `static/index.html` | Click-through of every flow; progress, confirmations, errors |
| 7 | End-to-end + docs | Final review of the whole design | `README.md`, fixes | Full end-to-end run through the API/UI; final QA report |

## Acceptance test

Create a sample DB that includes: an extension, multiple schemas (one with spaces in its name),
enum / domain / composite / range types, sequences, identity and serial columns, a generated column,
a collation override, a range-partitioned table with a DEFAULT partition, table inheritance, an
UNLOGGED table with reloptions, a self-referencing FK, plpgsql and SQL functions, a function that
takes a table row type, a `BEGIN ATOMIC` function, a procedure, an aggregate, views depending on
views, a materialized view with a unique index, a trigger, a rule, RLS with a policy, comments with
quotes, grants to a role, and data with tabs, newlines, backslashes, NULLs and bytea.

Then: back up → restore into a new DB → verify:
- `pg_dump -s` of both databases is identical after removing comment lines and sorting
- row counts match for every table
- an md5 of a large table's rows matches
- sequence `nextval()` values match

Also test:
- **Read-only user**: create a role with only `pg_read_all_data` (or CONNECT/USAGE/SELECT on PG 12/13);
  the backup succeeds, and the pre-check reports the RLS table it cannot bypass.
- **No writes**: compare `pg_stat_database.tup_inserted`/`tup_updated`/`tup_deleted`/`temp_files`
  on the source before and after — no rows changed (`xact_commit` rises for read-only transactions
  too, so it is not compared).
- **Lock timeout**: hold `LOCK TABLE ... IN ACCESS EXCLUSIVE MODE` in another session; the backup
  fails cleanly after the timeout and leaves no `.part` file.
- **Partial backup**: set "latest 1000 rows" on the partitioned orders table and "schema only" on
  another table; restore succeeds with 0 errors, orders has exactly 1000 rows (the newest), the
  affected FKs exist as NOT VALID, the file name contains `_partial`, and the next backup of the same
  DB opens with those options pre-filled.

## Out of scope for now

Scheduled backups, roles and tablespaces (cluster-level), large objects (`pg_largeobject`),
foreign data wrappers, event triggers, encrypted password storage, and uploading backup files
through the browser.
