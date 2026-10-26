# PG Backup Studio — Architecture

_Status: Steps 0–2 done (QA PASS); Step 3 design in §10 (2026-10-26, awaiting user approval). Sections marked **(Step N)** are filled in when that step is designed._
Spec (single source of truth): `.claude/commands/pg-backup-studio.md`. Decisions: `docs/decisions/`.

## 1. Layout and module responsibilities

Code lives in the project root (see `docs/decisions/001-project-layout.md`). Dependency direction:
`app.py -> jobs.py, store.py, engine.*`; `jobs.py -> store.py, engine.*`; `engine.*` never imports
`app`, `jobs` or `store` (it receives plain dicts / callbacks), so the engine is testable alone.

| Module | Responsibility | Planned public API (signatures may be refined in the step's design) |
|---|---|---|
| `app.py` | Flask app, JSON API, serves `static/index.html`; binds `127.0.0.1` | `create_app() -> Flask`; `main()` (reads `PORT`, default 8000) |
| `jobs.py` | Background threads, progress state, cancel, live log (Step 3) | `start_job(kind, fn, **kw) -> str`; `get_job(id) -> dict`; `list_jobs() -> list[dict]`; `cancel_job(id) -> bool`; class `Job` / `Progress` |
| `store.py` | JSON files in `./data` with atomic write + lock (Step 3) | `load_servers()`, `save_server(d) -> dict`, `delete_server(id)`, `get_settings()`, `save_settings(d)`, `add_history(d)`, `list_history()`, `delete_history(id)`, `get_profile(server_id, db)`, `save_profile(...)`, `delete_profile(...)` |
| `engine/util.py` | Pure helpers (Step 1) | `lit(value) -> str` (SQL literal); `stmt(sql) -> str` (ensure trailing `;\n`); `topo_sort(nodes, edges) -> list` (stable, cycle error); `class Cancelled(Exception)` |
| `engine/db.py` | Connections and server/DB admin (Steps 1, 4) | `connect(server: dict, dbname=None, **kw) -> psycopg.Connection`; `server_info(conn) -> dict`; `list_databases(conn)`; `list_roles(conn)`; `create_database(...)`; `drop_database(...)`; `rename_database(...)`; `clone_database(...)`; `kill_connections(conn, db) -> int` |
| `engine/precheck.py` | Permission check, sizes, large flags, "latest N" index check (Step 2) | `run_precheck(server, dbname, ...) -> dict`, `run_check`, `check_permissions`, `order_index_check` (§9.1) |
| `engine/catalog.py` | One query function per object type, version-gated (Step 1) | `get_schemas(conn, schemas)`, `get_extensions`, `get_types`, `get_sequences`, `get_functions`, `get_tables`, `get_constraints`, `get_indexes`, `get_views`, `get_triggers`, `get_policies`, `get_acl`, … each `-> list[dict]` |
| `engine/backup.py` | Builds DDL + streams COPY into one file (Steps 1, 2) | `run_backup(server, dbname, options, progress, cancel_event) -> dict` |
| `engine/restore.py` | SQL splitter + executor (Step 1) | `split_sql(stream) -> Iterator[Statement]`; `run_restore(server, dbname, path, options, progress, cancel_event) -> dict` |
| `static/index.html` | Single-page UI, plain JS (Step 6) | — |

`progress` is a callback/object supplied by `jobs.py` (engine only calls it); `cancel_event` is a
`threading.Event` checked between objects and COPY chunks, raising `Cancelled`.

## 2. Data flow (high level)

**Backup** (Steps 1–2): `POST /api/backup` -> `jobs.start_job` (new thread) -> `backup.run_backup`:
1. One connection to the source DB; `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`;
   `SET search_path = pg_catalog, client_encoding = 'UTF8', statement_timeout = 0, lock_timeout = <opt>, row_security = off`.
2. Gate on `server_version_num` (refuse < 120000). Read all catalog objects (`catalog.py`), excluding
   `pg_*`/`information_schema` and `pg_depend.deptype IN ('e','i')`.
3. Write header + objects in the spec's 16-step order to `<file>.part` (gzip optional); COPY streamed in chunks.
4. `COMMIT` (read-only), close; rename `.part` -> final; on error/cancel delete `.part`. Result -> history.

**Restore** (Step 1): `POST /api/restore` -> job -> `restore.run_restore`: create/recreate target via
maintenance DB if requested -> connect to target -> `split_sql` streams statements -> execute each
(autocommit, or one transaction when chosen); COPY blocks via `cursor.copy()` in ~1 MB batches;
errors collected `{object, message, sql[:160]}`.

## 3. JSON storage (`./data/`, created at runtime, git-ignored) — formats in §10.2

| File | Content |
|---|---|
| `servers.json` | saved servers (password plain text, file mode 0600 where possible; never returned by API) |
| `settings.json` | output folder, large-table thresholds, … |
| `history.json` | backup/restore records (spec Feature 6) |
| `backup_profiles.json` | remembered backup options keyed by server id + DB name |

## 4. Job / progress model outline (detailed in §10.4–10.6)
Job = `{id, kind: backup|restore, status: running|done|failed|cancelled, phase, started, elapsed,
percent, eta, counters{...}, current, log[last 200], result, error}`; held in memory under a lock,
polled by `GET /api/jobs/<id>` (~700 ms). Backup weights: 20 % objects, 80 % data.

## 5. API contract
Placeholder: see spec section "JSON API (suggested)". Errors are `{"error": "..."}` with 4xx/5xx.
Step 0 implements only `GET /` -> `static/index.html`. Full contract is written in Step 5.

## 6. Conventions
- psycopg 3 only; identifiers via `psycopg.sql.Identifier` / `sql.SQL`; values as bind parameters.
- In parametrized catalog SQL write a literal `%` as `%%`.
- Catalog columns must exist in PG 12; newer ones (`prosqlbody`, `tgparentid`, `rngmultitypid`,
  PG 18 virtual generated columns) gated on `server_version_num`.
- Server binds `127.0.0.1` only. No `pg_dump`/`psql` in the app (tests may use them to verify).
- No secrets in any file in the repo; tests read the password from `PGPASSWORD`.
- Module docstring on every module; type hints on public functions.

## 7. Testing strategy
- Runner: `pytest` (`python -m pytest`), tests in `tests/`, fixtures SQL in `tests/fixtures/`.
- Connection: libpq env vars `PGHOST` (default `localhost`), `PGPORT` (`5432`), `PGUSER` (`postgres`),
  `PGPASSWORD` (no default). If the server is unreachable or auth fails, DB tests are **skipped**
  (not failed) with the reason; pure unit tests (splitter, `topo_sort`, `lit`) always run.
- Tools: `PG_BIN` env var, default `C:\Program Files\PostgreSQL\18\bin`; `pg_dump`/`psql` used only
  for verification (`pg_dump -s` comparison). Tests needing them skip if missing.
- Databases: fixture source `pgbs_test_src` (dropped if exists, created from `template0` UTF8, loaded
  from `tests/fixtures/sample_db.sql`, dropped at session end); restore targets `pgbs_test_restore_<suffix>`;
  test roles prefixed `pgbs_test_`. Only objects with these prefixes are ever dropped by tests.
- Fixture load: `psql -X -q -v ON_ERROR_STOP=1 -d pgbs_test_src -f tests/fixtures/sample_db.sql`
  (test-only use of psql). Must load with zero errors on PG 18. PG 14+ features (`BEGIN ATOMIC`,
  multirange) are wrapped in `DO` blocks that `EXECUTE` them only when
  `current_setting('server_version_num')::int >= 140000`, so the same file loads on PG 12–13.
- Fixture data uses `INSERT`/`generate_series` (no COPY blocks) and is deterministic (no `random()`,
  no `now()` in data) so md5 comparisons are stable.
- Cluster-level role `pgbs_test_reader` is created idempotently (`DO ... IF NOT EXISTS`) because roles
  are out of scope for backup; it must exist before restoring into the same cluster.

## 8. Step 1 design — Backup + restore engine (2026-10-25, approved; built; QA PASS)

**Goal.** An engine that backs a database up from the catalogs into one `.sql`/`.sql.gz` file
(full / schema-only / data-only, schema filter, privileges on/off) and restores such a file through
psycopg with progress, cancel and error collection, proven by a full round trip on PG 18.
**Out of scope (later steps):** precheck, lock-timeout option, speed limit, partial/"latest N" (Step 2);
jobs/store/history (Step 3); creating/dropping the target DB (Step 4 — Step 1 restores into an existing
DB, tests create it); API/UI. Decisions: `decisions/002-missing-role-privileges.md`,
`decisions/003-dependency-ordering.md`. Catalog facts marked "verified" were checked on PG 18.4.

### 8.1 Interfaces
| Module | Public API |
|---|---|
| `util.py` | `Cancelled(Exception)`; `EngineError(Exception)`; `BackupError`, `RestoreError`, `CycleError(.nodes)` (all `EngineError`); `check_cancel(ev: threading.Event \| None) -> None` (raises `Cancelled`); `lit(v) -> str` (None→`NULL`, bool, int/float, str with `'` doubled, standard strings); `ident(name) -> str` (always `"..."`, `"` doubled); `qname(schema, name) -> str`; `stmt(sql) -> str` (strip, exactly one trailing `;`, then `\n`); `topo_sort(nodes: Iterable[H], deps: Mapping[H, Iterable[H]], key: Callable[[H], Any] = repr) -> list[H]` (Kahn, ties by `key`, deps outside `nodes` ignored, raises `CycleError`) |
| `db.py` | `MIN_SERVER_VERSION = 120000`; `UnsupportedServerError(EngineError)`; `connect(server: dict, dbname: str \| None = None, *, autocommit=False) -> psycopg.Connection` (keys `host, port, user, password, sslmode, maintenance_db`; `dbname=None` → `maintenance_db` or `postgres`; `connect_timeout=10`, `application_name='PG Backup Studio'`, **`prepare_threshold=None`**); `server_info(conn) -> dict` `{version_num, version, user, is_superuser, database, host, port, encoding}`; `require_supported(conn) -> int` (raises below 120000, message names the version) |
| `catalog.py` | Readers `(conn, ctx) -> list[dict]`, `ctx = CatalogContext(version_num, schema_oids)`: `get_schemas, get_extensions, get_types, get_sequences, get_functions, get_aggregates, get_tables, get_columns, get_inherits, get_constraints, get_indexes, get_statistics, get_views, get_triggers, get_rules, get_policies, get_comments, get_acls, get_dependencies`, `get_sequence_values(conn, seqs)`, `scan_unsupported(conn, ctx) -> list[str]`; `load_all(conn, schemas) -> Catalog` (dataclass of all lists) |
| `backup.py` | `@dataclass BackupOptions(mode: 'full'\|'schema'\|'data' = 'full', schemas: list[str] \| None = None, privileges=True, gzip=False, out_dir='~/pg_backups', filename: str \| None = None)` + `from_dict()`; `build_plan(cat: Catalog, opts) -> list[PlanItem]` (pure: DDL text, ordering, deferral); `run_backup(server: dict, dbname: str, options, progress=None, cancel=None) -> dict` |
| `restore.py` | `@dataclass Statement(sql, kind: 'sql'\|'copy', label_kind, label_name, line, data: Iterator[bytes] \| None)`; `open_backup(path) -> (raw, reader)` (gzip by magic `1f 8b`; `raw.tell()` = compressed position); `split_sql(reader: BinaryIO) -> Iterator[Statement]`; `@dataclass RestoreOptions(on_error: 'continue'\|'stop' = 'continue', single_transaction=False)`; `run_restore(server, dbname, path, options, progress=None, cancel=None) -> dict` |

`PlanItem(phase: int, label_kind: str, label_name: str, sql: str | None, copy: CopySpec | None)`.
Backup result: `{file, size, mode, schemas, objects, tables, rows, duration_s, server_version, warnings: [str], partial: False}`.
Restore result: `{status: ok|completed_with_errors|failed|cancelled, statements, objects, rows, errors: [{object, sqlstate, message, sql, line}], warnings: [same shape], error_count, warning_count, duration_s}` (lists capped at 1000, counts exact).

### 8.2 Backup data flow (one thread, one connection, one snapshot)
1. `connect(autocommit=False)`; `require_supported`; `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`;
   `SET search_path = pg_catalog`, `client_encoding = 'UTF8'`, `statement_timeout = 0`,
   `idle_in_transaction_session_timeout = 0`, `row_security = off`, `DateStyle = ISO`, `IntervalStyle = postgres`
   (the last two make COPY text portable between servers with different settings).
2. `catalog.load_all`; `scan_unsupported` → warnings (8.4); `build_plan`.
3. Open `<out_dir>/<db>_YYYYMMDD_HHMMSS.sql[.gz].part` in **binary** mode (`gzip.open(..., 'wb', 6)` when
   gzip). Never text mode: Windows would write `\r\n` and corrupt COPY data. Write header (comment block,
   warnings, the spec's SET lines), then each `PlanItem` after `-- Type: <KIND>; Name: <name>`.
4. COPY: `with cur.copy("COPY <qname> (<non-generated cols>) TO STDOUT") as cp: for chunk in cp:` write,
   `rows += chunk.count(b'\n')`, `check_cancel`, throttled progress; then `\.\n`. No column list when
   the table has zero non-generated columns (verified: zero-column rows are empty lines).
5. Sequence values: `SELECT last_value, is_called FROM <seq>` per sequence →
   `SELECT pg_catalog.setval('<qname>', <last_value>, <is_called>);`.
6. `COMMIT`, close, `os.replace(part, final)`. On any exception/cancel: `conn.cancel_safe()` if a COPY
   is open, close conn, delete `.part`, re-raise (`Cancelled` unchanged; DB errors as
   `BackupError("<kind> <name>: <message>")`, e.g. RLS or permission errors).

### 8.3 Emission order (spec's 16 steps) per mode
| # | Phase | full | schema | data |
|---|---|---|---|---|
| 1–7 | schemas; extensions (all except plpgsql, even if their schema is not selected — that schema gets `CREATE SCHEMA IF NOT EXISTS`); types; sequences; early functions; tables; `OWNED BY` | yes | yes | – |
| 8 | table data (FK-topological, partitions mapped to their root, self-FK ignored) | yes | – | yes, with trigger guard (see risks) |
| 9 | `setval` for all sequences incl. identity | yes | – | yes |
| 10–12 | PK/unique/exclusion/check; indexes + `CLUSTER ON` + replica identity + extended statistics; FKs | yes | yes | – |
| 13 | late group, topo-sorted: views, matviews, aggregates, late functions, deferred items | yes | yes | `REFRESH MATERIALIZED VIEW` for populated matviews |
| 14–16 | matview indexes; triggers + state, rules + state, RLS flags, policies; comments / owner / ACL after each object | yes | yes | – |

Matviews: `WITH DATA` when `relispopulated` (they come after phase 8, so data exists), `WITH NO DATA`
when not populated or in schema-only. Constraints, FKs and triggers come after data, so data order
cannot break a full restore: the self-FK and the FK into partitioned `orders` are validated once when
added (phase 12), after the PK on `orders` (phase 10). Constraints use `ALTER TABLE` (not `ONLY`) so
PK/CHECK recurse to partitions and inheritance children as in the source (their clones are skipped).

### 8.4 Catalog queries (selected schema OIDs; every object filtered by `NOT EXISTS pg_depend deptype IN ('e','i')` on itself)
| Kind | Source | Notes / version gates |
|---|---|---|
| schemas | `pg_namespace` | exclude `pg_*`, `information_schema`; `nspowner`, `nspacl` |
| extensions | `pg_extension` | skip `plpgsql`; `WITH SCHEMA`; no VERSION |
| enums | `pg_type typtype='e'`, `pg_enum ORDER BY enumsortorder` | |
| domains | `typtype='d'`: `format_type(typbasetype, typtypmod)`, `typcollation`, `typdefault`, `typnotnull`; checks `pg_constraint contypid` | checks as `ALTER DOMAIN ... ADD CONSTRAINT` |
| ranges | `pg_range`: subtype, non-default opclass, collation, canonical, subdiff | `rngmultitypid` → `MULTIRANGE_TYPE_NAME` only **>= 140000** |
| composites | `typtype='c'` whose `typrelid` has `relkind='c'`; `pg_attribute` | |
| sequences | `pg_sequence` + `pg_class` | identity sequences (dep `'i'`) excluded here, read separately for identity DDL + setval; `UNLOGGED` **>= 150000** |
| functions | `pg_proc prokind IN ('f','p','w')`, `pg_get_functiondef` | late if `prosqlbody IS NOT NULL` (**>= 140000**) or bumped by 8.5 |
| aggregates | `prokind='a'` + `pg_aggregate` (`aggkind='n'`): SFUNC, STYPE, FINALFUNC(+EXTRA), COMBINE/SERIAL/DESERIALFUNC, INITCOND, SORTOP, PARALLEL | ordered-set / moving (`aggmtransfn<>0`) → skipped with warning |
| tables | `pg_class relkind IN ('r','p')`: `relpersistence`, `reloptions`, `relam` (`USING` if not heap), `pg_get_partkeydef`, `relispartition` + `pg_get_expr(relpartbound, oid)`, `relreplident`, `relrowsecurity`, `relforcerowsecurity` | |
| columns | `pg_attribute attnum>0 AND NOT attisdropped`: `format_type`, collation if `<> typcollation`, `pg_attrdef`, `attidentity`, `attgenerated`, `attnotnull`, `attislocal`, `attacl` | `attgenerated='v'` **>= 180000**; on 18 read `pg_constraint contype='n'` names, emit `CONSTRAINT x NOT NULL` only if name is not `<table>_<col>_not_null` (verified default naming; syntax valid on 12+) |
| inheritance | `pg_inherits ORDER BY inhseqno` | edges added to the table topo sort |
| constraints | `pg_constraint contype IN ('p','u','x','c','f')`, `pg_get_constraintdef` | skip `conislocal=false`, `conparentid<>0`; never `'n'` (PG 18 NOT NULL, verified) or `'t'` |
| indexes | `pg_index` + `pg_class`, `pg_get_indexdef`, `indisclustered` | constraint-backed excluded by `'i'`; skip `relispartition`; `ON ONLY`→`ON`; matview indexes → phase 14 |
| statistics | `pg_statistic_ext`, `pg_get_statisticsobjdef` | phase 11 |
| views / matviews | `relkind IN ('v','m')`, `pg_get_viewdef(oid)` minus trailing `;`, `reloptions`, `relispopulated` | |
| triggers | `pg_trigger NOT tgisinternal`, `pg_get_triggerdef`, `tgenabled` | `tgparentid = 0` **>= 130000** (PG 12 clones are `tgisinternal`); state 'D'→`DISABLE TRIGGER`, 'R'→`ENABLE REPLICA`, 'A'→`ENABLE ALWAYS` (verified 'D' on fixture) |
| rules | `pg_rewrite rulename <> '_RETURN'`, `pg_get_ruledef`, `ev_enabled` | |
| policies | `pg_policy`: `polcmd`, `polpermissive`, `polroles` (0 = PUBLIC), `pg_get_expr(polqual / polwithcheck, polrelid)` | |
| comments | `pg_description` by `classoid, objoid, objsubid` | incl. column, constraint (`ON` table / `ON DOMAIN`), index, trigger, rule, policy |
| ACL | `aclexplode()` on `nspacl/relacl/attacl/proacl/typacl` | REVOKE PUBLIC, REVOKE owner, one GRANT per entry; grantor not reproduced |
| dependencies | `pg_depend deptype IN ('n','a')` | resolved per 8.5 |
| unsupported | counts in selected schemas: collations, operators / opclasses / opfamilies, casts using user functions, text-search objects, conversions, `pg_default_acl`, foreign tables, publications, user base types, large objects | result warnings + header block "NOT INCLUDED" |

Quoting: names come from the server (`oid::regclass::text`, `oid::regprocedure::text`, `format_type`,
qualified because of `search_path = pg_catalog`, verified) or from `util.ident/qname`. Literal `%` is
`%%` in parametrized queries. Locks: only the `AccessShareLock` of `COPY TO` / `pg_get_*def`. No writes.

### 8.5 Dependency ordering (decision 003)
Each object has a key and a default phase. `get_dependencies` pairs are resolved on both sides:
`pg_type` with `typrelid<>0` → that relation (row types); array / multirange type → element / range;
`pg_rewrite '_RETURN'` → its view; `pg_attrdef` → that column default; domain `pg_constraint` → that
domain check; extension-owned or other catalogs (namespace, language, collation, opclass) → ignored.
Dropped edges: self edges, sequence→table `'a'` (OWNED BY, verified), identity `'i'`. Walking in
topological order, an object that depends on something emitted later is **deferred** to the late
group; deferrable: functions, composites, domain checks, column defaults (`ALTER TABLE ... ALTER COLUMN
SET DEFAULT`), check constraints, indexes. A non-deferrable case (e.g. generated column calling a late
function) → warning "may not restore". Each phase is emitted in `topo_sort` order, ties by
`(schema, name)`; a cycle raises `CycleError` naming the objects. An edge into an unselected user
schema → warning. Verified on PG 18: `customer_label(app.customers)` → `pg_type(typrelid=app.customers)`
→ late; `atomic_double` has no refs but `prosqlbody` → late; `view_b` → `view_a` → `customers` via
`pg_rewrite`; `sum_squares` → `sum_sq_step`; range constructors are `'i'` and excluded.

### 8.6 Splitter state machine (`split_sql`)
Reads bytes with `readline()`, decodes each line as UTF-8, scans char by char; state persists across lines.
| State | Enter on | Leave on |
|---|---|---|
| NORMAL | start | `;` at body depth 0 → yield statement |
| SQ | `'` (also `U&'`) | `'` not followed by `'` |
| ESQ | `E'`/`e'`, no identifier char before `E` | unescaped `'` (`\x` skips one char; `''` stays) |
| DQ | `"` | `"` not followed by `"` |
| DOLLAR(tag) | `$tag$`/`$$`, no identifier char before `$`, tag not starting with a digit (`$1` stays NORMAL) | the same `$tag$` |
| LINE_COMMENT | `--` | end of line; at statement start `-- Type: K; Name: N` sets the label |
| BLOCK(depth) | `/*` | matching `*/` (inner `/*` adds depth) |
| COPY_DATA | yielded statement matches `^COPY .+ FROM stdin$` (case-insensitive) | line exactly `\.` |

Body depth counts only when the statement starts `CREATE [OR REPLACE] FUNCTION|PROCEDURE`: whole words
`BEGIN`/`CASE` +1, `END` −1, in NORMAL state only. COPY `Statement.data` yields ~1 MB batches of raw
line bytes; unconsumed data is drained on the next `next()`. EOF inside COPY data or an open quote →
`RestoreError("file truncated at line N")`. Empty data lines are valid rows.

### 8.7 Restore executor
`autocommit=True`; with `single_transaction` one transaction and `on_error` forced to `stop`.
Statements via `cur.execute(sql)` with **no params** (no `%` interpretation). COPY via `cur.copy(sql)`
writing the batches; on error the rest of that block is skipped. Error →
`{object: label, sqlstate, message: diag.message_primary, sql: sql[:160], line}`; `stop` ends the run
(`failed`, single transaction rolled back). Privilege statements (`GRANT`, `REVOKE`,
`ALTER ... OWNER TO`) failing with SQLSTATE `42704` (verified for missing roles) → **warning**, run
continues in every mode; in single-transaction mode they run inside a savepoint (`conn.transaction()`).
Connection loss → fatal `failed`. Cancel is checked between statements and COPY batches; single
transaction rolls back, otherwise the run stops and reports what was applied.

### 8.8 Progress callback contract
`progress(event: str, data: dict) -> None`; called on the worker thread; must be fast and not raise;
`None` = no-op. Engine sends raw counters; `jobs.py` (Step 3) computes % and ETA.
| Event | Backup `data` | Restore `data` |
|---|---|---|
| `phase` | `{name}` | `{name}` |
| `totals` | `{objects, tables, est_rows, data_bytes}` (`reltuples`, `pg_table_size`) | `{file_size}` |
| `object` | `{kind, name, objects_done}` | `{kind, name, objects_done}` |
| `table_start` / `table_done` | `{name, est_rows, size}` / `{name, rows}` | – |
| `table_progress` | `{name, rows, bytes_copied, bytes_written}` (max 4/s) | – |
| `position` | – | `{pos, rows_loaded, errors, warnings}` (max 4/s; `pos` = raw file offset) |
| `log` / `warning` / `error` | `{level, msg}` | `{level, msg}` / error dict |

### 8.9 Acceptance-test plan (QA on PG 18: `tests/test_engine_unit.py`, `tests/test_roundtrip.py`)
- Unit: `lit/ident/qname/stmt`; `topo_sort` (ties, cycle); `build_plan` order on a fake `Catalog`
  (row-type function and BEGIN ATOMIC function after `customers`, view_b after view_a); splitter: `''`,
  `E'\''`, `"a""b"`, `$$`/`$f$` with `;` inside, `$1`, nested comments, `--` containing `;`,
  BEGIN ATOMIC with CASE…END, `CREATE VIEW` with CASE, COPY with tab/`\N`/backslash/empty lines, truncated file.
- Round trip (plain and gzip): back up `pgbs_test_src`, restore into `pgbs_test_restore_full` → 0 errors, 0 warnings;
  `pg_dump -s` of both with lines starting `--`, `\restrict `, `\unrestrict ` and blank lines removed, sorted → identical
  (PG 18.4 `pg_dump` emits random-key `\restrict` lines, verified); `count(*) FROM ONLY` equal for every
  `relkind='r'`; md5 of `string_agg(t::text, '|' ORDER BY pk)` equal for `orders`, `app.edge`, `app.customers`;
  every sequence `(last_value, is_called)` equal, then `nextval()` equal; matview populated with equal
  rows; `tgenabled` equal; identity ALWAYS values kept (COPY FROM needs no `OVERRIDING`, verified); file has no `\r\n`.
- Schema-only: `pg_dump -s` identical, all tables empty. Data-only: restore schema-only file, then
  data-only file → counts/md5/sequences equal, `audit.log` not inflated, `customers_audit_disabled` still `D`.
- Options: privileges off → no `GRANT`/`REVOKE`/`OWNER TO`; `schemas=['app']` → no `"sales data"` objects, extensions present, restore 0 errors.
- Restore modes with one injected bad statement: continue (1 error, rest applied), stop (stops there),
  single transaction (target unchanged). Missing role: rename `pgbs_test_reader` in a copy of the file →
  warnings > 0, errors 0, also in single-transaction mode.
- Cancel at first `table_start` → `Cancelled`, no `.part`, no final file; restore cancel in single transaction → empty target.
- Version gates: queries built for `version_num=120000` contain no `prosqlbody`, `tgparentid`,
  `rngmultitypid`, `'v'` generated (real PG 12–17 runs remain an open item).

### 8.10 Risks and edge cases
| Risk | Handling |
|---|---|
| Object kinds the spec does not cover (operators, casts, collations, text search, default privileges, …) silently missing | `scan_unsupported` → warnings in result + file header (pending user decision) |
| Data-only restore: user triggers fire during COPY (fixture `customers_audit` would add 50 `audit.log` rows) | Around each table's COPY: `ALTER TABLE ... DISABLE TRIGGER <name>` for its enabled user triggers, then restore each exact state (needs table owner, not superuser) |
| Data-only with FK cycles | FK-topological COPY order; cycle → warning, name order |
| Grants/owners to roles missing on target | warnings (decision 002) |
| Sequences are not MVCC: values may be slightly ahead of the snapshot | Same as pg_dump; harmless |
| Grantor other than owner; column storage/compression/statistics targets; typed tables; per-partition defaults | Not reproduced in Step 1; listed in README limitations; `pg_dump -s` test shows any fixture impact |
| Only PG 18 available locally | Gates unit-tested; PG 12–17 runs are an open item |
| psycopg auto-prepare of repeated statements | `prepare_threshold=None` |

## 9. Step 2 design — Pre-check, safety, partial backups (2026-10-26, approved; built; QA PASS)

**Goal.** Before a backup, show what the login cannot read, how big each table is and what the backup
will cost; let the user pick Full / Latest N / Schema only per table; make the backup fail fast and
cleanly on locks, optionally throttle it, and keep partial backups restorable (NOT VALID FKs, `_partial`
name, header block, `partial` flag). Also fix OBS-1 (cancel during a lock wait or long statement).
**Out of scope:** `store.py` read/write of `backup_profiles.json` (Step 3; this step fixes the JSON shape
and the engine helpers), jobs, API, UI, DB management. Decision: `decisions/004-step2-locking-partial-fks.md`.
Facts marked "verified" were checked on PG 18.4 on 2026-10-25.

### 9.1 Interfaces
| Module | New / changed public API |
|---|---|
| `precheck.py` | `@dataclass Thresholds(large_mb: float = 500, large_rows: int = 1_000_000)` + `from_settings(d)`; `@dataclass TableOption(schema, table, data: 'full'\|'latest'\|'schema' = 'full', order_by: str \| None = None, limit: int = 100_000)` + `from_dict/to_dict/validate`; `run_precheck(server, dbname, schemas=None, mode='full', tables: list[dict] \| None = None, thresholds=None, lock_wait_s=2.0, cancel=None) -> dict` (own connection); `run_check(conn, ...)` (same, on an open conn); `check_permissions(conn, reads: ReadSet) -> list[dict]`; `order_index_check(conn, relid: int, column: str) -> dict`; `read_set(relations, mode, options) -> ReadSet` (pure); `reconcile_table_options(relations, saved: list[dict]) -> tuple[list[TableOption], list[str]]` (pure: drops options for missing tables and for partitions, returns their names) |
| `catalog.py` | `list_relations(conn, schemas) -> list[dict]` `{oid, schema, name, qname, kind ('r'\|'p'\|'S'), is_partition, root_oid, owned_by_relid, can_lock}` — lock-free (no `pg_table_size`, no `pg_get_expr`) |
| `db.py` | `cancel_on_event(conn, ev: threading.Event \| None, poll=0.2)` context manager (watcher thread → `conn.cancel_safe(timeout=5)`) |
| `util.py` | `BackupBlockedError(BackupError)` with `.problems: list[dict]`; `LockTimeoutError(BackupError)` with `.relation` |
| `backup.py` | `BackupOptions` gains `lock_timeout_s: float = 10` (0 = wait forever, max 3600), `speed_limit_mb_s: float \| None = None` (≥ 0.1; MiB/s of COPY data), `tables: list[dict] = []` (TableOption dicts; only non-full needed) + `to_dict()`; `CopySpec` gains `source: sql.Composable \| None` (server-side query when not plain `COPY t`) and `option`; result gains `partial, partial_tables, not_valid_fks, skipped_fks, lock_timeout_s, speed_limit_mb_s` |
| `restore.py` | execution wrapped in `db.cancel_on_event` (OBS-1 for restore too) |

`ReadSet = {tables: [{oid, qname, only}], sequences: [{oid, qname}], schemas: {oid}}` = exactly what the
backup will `COPY`/`SELECT`: leaves of full roots, latest-N roots, nothing for schema-only roots; every
sequence except those owned (OWNED BY / identity) by a schema-only table that the user cannot `SELECT`
(those skip `setval` with a warning). `mode='schema'` → empty.

### 9.2 Pre-check (`run_precheck`) — one short autocommit connection
Session: `default_transaction_read_only = on`, `search_path = pg_catalog`, `lock_timeout = <lock_wait_s>`,
`statement_timeout = '60s'`, inside `cancel_on_event`. Each query is its own transaction (no locks kept).
1. `server_info` + `rolbypassrls`; `pg_has_role('pg_read_all_data', 'MEMBER')` only if `>= 140000`.
2. `list_relations`; roots = tables with `NOT is_partition` (partitions are never listed; a partitioned
   root is one logical table). Options = `reconcile_table_options(relations, tables)`.
3. Per root over its leaves (`pg_partition_tree(oid) WHERE isleaf` for kind 'p', PG 12+; else the table
   itself): `size = Σ pg_table_size`, `est_rows = Σ GREATEST(reltuples, 0)`, `rows_unknown = bool_or(reltuples
   < 0)` (PG 14+ never analysed). `pg_table_size` **waits for locks** (verified: 55P03 under ACCESS
   EXCLUSIVE), so leaves with an `AccessExclusiveLock` in `pg_locks` (granted or waiting, this DB) use the
   lock-free `(relpages + toast relpages) * current_setting('block_size')::bigint`, `size_exact = false`;
   if the query still gets 55P03, rerun with estimates only. `pg_locks` is readable by any role (verified).
4. `large = size > large_mb·2^20 OR est_rows > large_rows`; `large_reason ⊆ ['size', 'rows']`.
5. Ordering candidates: PK columns (`conkey`) + columns whose type (or domain base type) is `date`,
   `timestamp`, `timestamptz`, in `attnum` order. Default = first timestamp-like, else first PK column,
   else `null` (user picks; any existing column is accepted). Each candidate carries `order_index_check`.
6. `problems = check_permissions(conn, read_set(...))` (9.3); `locks = [{qname, mode, granted, pid}]` for
   `AccessExclusiveLock`s on listed tables (warning, not blocking).
7. Impact over the ReadSet with current options: `data_bytes`, `est_rows` (latest N: `min(N, est_rows)`
   rows, `size·min(1, N/est_rows)` bytes), full-backup totals, `largest` (top 10 by size).

Result: `{server{version_num, version, user, is_superuser, bypassrls, read_all_data}, thresholds,
tables:[{schema, name, qname, kind:'table'|'partitioned', partitions, size, size_exact, est_rows,
rows_unknown, large, large_reason, option{data, order_by, limit}, order_columns:[{name, type, pk,
timestamp_like, not_null, index{...}}], default_order_column}], problems, blocking, locks,
ignored_options:[names], impact{tables, data_bytes, est_rows, full_data_bytes, full_est_rows,
largest:[{qname, size, est_rows}]}, notes:["The backup only reads, but it adds load: run it off-peak or
against a read replica."]}`.

`order_index_check(conn, relid, column)` → `{indexed, usable, index, null_safe, message}`: btree index with
`indkey[0] = attnum`, `indpred IS NULL`, `indisvalid` (a partitioned index on a 'p' root is valid only
when every partition has one). **Verified:** `ORDER BY c DESC NULLS LAST LIMIT n` never uses a default
ASC index, even on a NOT NULL column; `ORDER BY c DESC` does (Index Only Scan Backward). So `null_safe` =
`attnotnull` and, on `>= 180000`, the column's `contype='n'` constraint is `convalidated` (PG 18 `NOT NULL
NOT VALID` keeps `attnotnull = true` with NULLs present — verified). `usable` = indexed AND (null_safe OR
`indoption[0] IN (1, 2)`, i.e. DESC NULLS LAST or ASC NULLS FIRST). Messages: "no index starts with c:
the whole table is sorted" / "index i cannot be used because c allows NULLs; an index on (c DESC NULLS
LAST) would be used". The pre-check never creates anything.

### 9.3 Permission check (shared by pre-check and backup)
One query over the ReadSet oids: `has_schema_privilege(nsp, 'USAGE')`; tables `has_table_privilege(oid,
'SELECT') OR` every column `has_column_privilege(oid, attnum, 'SELECT')`; sequences
`has_sequence_privilege(oid, 'SELECT')`; RLS `row_security_active(oid)` (true exactly when policies would
apply to this user: not superuser, no BYPASSRLS, not owner or FORCE — verified for owner, FORCE and
`pg_read_all_data`; takes no lock). Problem = `{kind: 'schema_usage'|'table_select'|'sequence_select'|'rls',
schema, name, qname, message, fixes: ['exclude_schema'] (+ 'schema_only' for tables)}`.
`blocking = bool(problems)`. Mode `schema` → none (catalog reads need no rights).
**Backup when blocked:** `run_backup` re-runs the check before the snapshot and raises
`BackupBlockedError` listing every problem; nothing is locked or written. No "ignore" switch: the user
excludes the schema or sets the table to Schema only (which makes the backup partial).

### 9.4 Backup data flow changes (order matters)
1. Validate options (`TableOption.validate`, duplicates, ranges) → `BackupError`. Connect;
   `require_supported`; `cancel_on_event(conn, cancel)` for the whole run.
2. **Pre-snapshot transaction** (rolled back): `list_relations`; resolve options (unknown table or a
   partition → warning "option ignored"); `latest` whose `order_by` column no longer exists →
   `BackupError` naming table and column; `check_permissions` → `BackupBlockedError`. `conn.rollback()`.
3. `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`; `SESSION_SETTINGS` + `SET lock_timeout = '<ms>ms'`.
4. **Lock before the snapshot** (verified: `SET` and `LOCK` take no snapshot; the first `SELECT` does):
   `LOCK TABLE ONLY <qname> IN ACCESS SHARE MODE`, one statement per `r`/`p` table of the selected schemas
   with `can_lock` (= `has_table_privilege(oid, 'SELECT')`; LOCK without SELECT is 42501, verified).
   55P03 → `LockTimeoutError("Table X is locked by another session (for example ALTER TABLE, TRUNCATE,
   VACUUM FULL, REINDEX); gave up after 10 s. Nothing was written. Try again later or raise the lock
   timeout.")`; 42P01 (dropped meanwhile) → rollback, redo steps 2–4 once; 53200 → "too many tables for
   max_locks_per_transaction". Why: (a) **verified:** a concurrent `TRUNCATE` or table rewrite (`ALTER
   COLUMN TYPE`) after our snapshot makes `COPY` return **0 rows with no error**; (b) Step 1 catalog
   readers (`pg_table_size`, `pg_get_expr`) already take brief AccessShare locks and wait (verified for
   the `tables` and `columns` readers), so today a locked table hangs inside "reading the catalog" with
   no table name; (c) every wait now happens before the `.part` file exists.
5. `catalog.load_all` (takes the snapshot). A read-set table not locked in 4 (created in the window) is
   locked now. Any other 55P03 → `LockTimeoutError("an object is locked (while reading <step>)")`.
6. `build_plan(cat, opts)` with options (9.5), header (9.6), open `.part`, write, `COMMIT`, rename.
7. Errors: any `psycopg.Error` while `cancel.is_set()` → `Cancelled`; otherwise as in Step 1. `.part`
   removed in every failure path (unchanged).

### 9.5 Plan changes for per-table options (`mode != 'schema'`; options ignored in schema mode)
- **Data items** per root: `full` → existing leaf COPYs; `schema` → nothing for root and leaves; `latest`
  → one item, label `TABLE DATA <root>`, file side `COPY <root> (<cols>) FROM stdin;` (rows route to
  partitions on restore), source side `COPY (SELECT <cols> FROM [ONLY] <root> ORDER BY <col> DESC[ NULLS
  LAST] LIMIT <N>) TO STDOUT` composed with `sql.Identifier` / `sql.Literal(int)`; `ONLY` for kind 'r'
  (inheritance children are separate tables), none for 'p'; `NULLS LAST` omitted when `null_safe` (same
  rows, index usable). Zero columns → `SELECT FROM`. FK-topological order of roots unchanged. Data mode +
  latest on a 'p' root: the trigger guard covers the root and every leaf.
- **FK NOT VALID** (decision 004 §2): FK on table A (root of `relid`) referencing B (root of `frelid`) gets
  ` NOT VALID` when B's option is not `full` **and** A's option is not `schema` (A has rows). Not appended
  when `def` already ends with `NOT VALID` or contains `NOT ENFORCED` (PG 18). If A is partitioned and
  `version_num < 180000` (NOT VALID FKs on partitioned tables arrived in PG 18), the FK is **omitted**:
  warning + the header lists its full `ALTER TABLE ... ADD CONSTRAINT ...;` to run later.
- **Sequences:** `setval` kept for all (a schema-only table restarts from the source value — harmless).
- **Progress:** `CopySpec.est_rows = min(N, est_rows)` (N when unknown), size scaled as in 9.2;
  schema-only tables have no CopySpec, so they are outside the totals.
- `partial = mode != 'schema' and any effective non-full option`. Data mode + partial → warning "a
  data-only restore into a schema with valid FKs fails for rows whose parents were left out".

### 9.6 File name, header, result
Auto name `<db>_YYYYMMDD_HHMMSS_partial.sql[.gz]` (an explicit `filename` is used as given). Header block
after the warnings (one line each; names via `_one_line`):
```
-- PARTIAL BACKUP: some tables do not contain all rows.
--   "sales data".orders: latest 1000 rows by ordered_at (newest first)
--   app.secrets: schema only (no data)
-- Foreign keys written as NOT VALID (check later: ALTER TABLE ... VALIDATE CONSTRAINT ...):
--   "order lines_order_id_ordered_at_fkey" ON "sales data"."order lines"
```
Result additions: `partial`, `partial_tables: [{table, data, order_by, limit}]`, `not_valid_fks`,
`skipped_fks`, `lock_timeout_s`, `speed_limit_mb_s`.

### 9.7 Speed limit and cancel (OBS-1)
- `_Throttle(limit_mb_s, cancel)`, reset at each table's first chunk; after each flush `due = start + bytes
  / (limit · 2^20)`; while `now < due`: `cancel.wait(min(due - now, 0.25))`, raise `Cancelled` if set.
  Counts uncompressed COPY bytes (= load on the source). No limit → no sleeps.
- `db.cancel_on_event`: daemon thread `while not stop.wait(0.2): if ev.is_set(): conn.cancel_safe(timeout=5); return`;
  stopped in `finally` and before `COMMIT`. **Verified:** `cancel_safe()` from another thread ends a
  `LOCK` wait with `QueryCanceled` (57014) at once and the connection stays usable. A cancel that lands
  between statements is harmless (the event is checked next anyway). Same wrapper in `run_restore`
  (the cancelled statement is not applied; single transaction rolls back) and `run_precheck`.

### 9.8 Profile JSON (`data/backup_profiles.json`; read/write by `store.py` in Step 3)
```json
{"version": 1, "profiles": {"<server_id>": {"<db name>": {"saved_at": "2026-10-25T14:03:00+06:00",
  "options": {"mode": "full", "schemas": null, "privileges": true, "gzip": false, "out_dir": "~/pg_backups",
    "lock_timeout_s": 10, "speed_limit_mb_s": null,
    "tables": [{"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": 1000},
               {"schema": "app", "table": "secrets", "data": "schema"}]}}}}}
```
Nested keys (no escaping of dots/quotes in names). `options` = `BackupOptions.to_dict()` minus `filename`;
`schemas: null` = all (new schemas included); only non-full tables stored. Loading = pre-check with
`tables=saved` → `ignored_options` names dropped entries; new tables are Full. Thresholds live in
`settings.json`: `{"large_table_mb": 500, "large_table_rows": 1000000}` → `Thresholds.from_settings`.

### 9.9 MAINTAIN Option A (only if the user chooses it; decision 004 §5)
In `_acl`: `FULL = {TABLE: {SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER} ∪ {MAINTAIN if
version_num >= 170000}, SEQUENCE: {SELECT, UPDATE, USAGE}, SCHEMA: {USAGE, CREATE}, FUNCTION/PROCEDURE:
{EXECUTE}, TYPE/DOMAIN: {USAGE}}` (verified via `acldefault`). A grantee group whose set equals
`FULL[keyword]` is emitted as `GRANT ALL ON ...` (same grant option). Column ACLs unchanged. Effect:
`REVOKE ALL ... FROM owner; GRANT ALL ... TO owner` works on PG 12–16; an explicit `GRANT MAINTAIN` to
another role still fails there (README).

### 9.10 Risks and edge cases
| Risk | Handling |
|---|---|
| Up-front locks hold off DDL (and a waiting ALTER queues later queries) for the whole backup | Same as `pg_dump`; pre-check note + lock list; README: run off-peak |
| Many tables: one LOCK round trip each; lock table overflow (53200) | ≈1 s per 5000 tables locally; clear message; batching later |
| Column-only SELECT grants: passes the check but cannot be LOCKed | Not locked (`can_lock` false); COPY works; the TRUNCATE race remains for that table only (README) |
| Privileges change between check and snapshot | COPY "permission denied" → BackupError, no `.part` |
| Ties on the ordering column | Exactly N rows; which tied rows is unspecified (README) |
| Nullable ordering column without a suitable index | Pre-check warning with the suggested index (never executed) |
| `reltuples` -1 / 0 (never analysed) | `rows_unknown`; size still flags large; estimates use N |
| PG 12–17 partitioned table referencing a partial table | FK omitted + warning + header statement (9.5) |
| Saved option for a dropped column | Backup refuses naming table and column; pre-check shows it |

### 9.11 Acceptance-test plan (QA on PG 18; fixture `pgbs_test_src`, `ANALYZE` it in setup)
1. **Read-only user** (CONNECT + `pg_read_all_data` only): pre-check `blocking` with exactly one `rls`
   problem `app.secrets`; backup raises `BackupBlockedError` naming it, no `.part`; with `app.secrets` =
   schema → backup OK, `partial`, restore 0 errors. Role with USAGE on `app` and some SELECTs: every missing
   table / sequence / schema listed once with the right `kind`; `mode='schema'` → no problems.
2. **No writes:** `tup_inserted/updated/deleted` and `temp_files` of `pg_stat_database` unchanged across
   pre-check, partial backup and a lock-timeout failure.
3. **Lock timeout:** other session holds ACCESS EXCLUSIVE on `"sales data".orders_2024q1`; backup with
   `lock_timeout_s=2` fails in 2–4 s with `LockTimeoutError` naming it; no `.part`, no file; same when that
   table is schema only. Pre-check under the same lock returns in < 5 s with `size_exact=false` and the lock listed.
4. **Up-front lock:** during the backup's `catalog` phase, another session's `TRUNCATE` (lock_timeout 1 s)
   of a data table fails with 55P03.
5. **Cancel (OBS-1):** `lock_timeout_s=0` + held lock, cancel after 1 s → `Cancelled` within 2 s, no `.part`,
   no leftover backend; the xfail `test_backup_cancel_while_waiting_for_table_lock` now passes. Cancel
   during a throttled copy → `Cancelled` < 1 s. Restore of a file with `SELECT pg_sleep(30);`, cancel → `cancelled` < 2 s.
6. **Partial (spec):** orders latest 1000 + another table schema only → name has `_partial`, header block,
   result `partial: true` with lists; restore 0 errors; orders has 1000 rows = the source ids of `ORDER BY
   ordered_at DESC LIMIT 1000`; schema-only table empty; `"order lines"` FK `convalidated = false`;
   `orders → app.customers` FK still validated; totals `est_rows` = 1000 + the full tables.
7. Latest N on `app.vehicle` (inheritance) copies only its own rows; `app.car` unchanged. Nullable column with
   NULLs → NULL rows come last; NOT NULL column → the source query has no `NULLS LAST`.
8. `order_index_check`: none (orders.ordered_at → warning), ASC on NOT NULL → usable, ASC on nullable → not
   usable + message, `DESC NULLS LAST` → usable, PG 18 `NOT NULL NOT VALID` → not null_safe.
9. Pre-check: orders listed once, `partitions=3`, summed size/rows, no leaves listed; custom thresholds
   (e.g. `large_rows=1000`) flag correctly; default order column `ordered_at`; impact changes with options.
10. Speed limit 1 MB/s on a ~5 MiB table → ≥ 4 s; no limit → no sleeps.
11. Options: bad `data`, `limit` 0, missing `order_by`, duplicates, speed 0.05, negative lock timeout →
    BackupError; dropped ordering column → BackupError naming it; `to_dict`/`from_dict` round trip;
    `reconcile_table_options` drops missing tables and partitions, new tables stay Full.
12. FK unit tests (fake Catalog): already NOT VALID unchanged; `NOT ENFORCED` unchanged; self-FK on a partial
    table → NOT VALID; `version_num=170000` partitioned referencing table → omitted + header statement.
13. Schema mode with options → not partial, no `_partial`; data mode + partial → warning.
14. (Option A only) owner ACLs emitted as `GRANT ALL`; round trip `relacl` identical; `pg_dump -s` diff 0.

## 10. Step 3 design — Storage and jobs (2026-10-26, awaiting approval)

**Goal.** Persist saved servers, settings, history and per-database backup profiles in `./data/*.json`
safely (atomic, thread-safe, corruption-tolerant, versioned), and run backups/restores as background
jobs that several clients can poll for a consistent progress snapshot (%, ETA, counters, 200-line log),
cancel cleanly, record in history and (after a successful backup) remember the options.
Also fixes BUG-S2-3/4/5 and one concurrency bug found in this review (§10.7).
**Out of scope:** Flask routes and HTTP status codes (Step 5), UI (Step 6), DB create/drop/recreate of
a restore target (Step 4; Step 3 restores into an existing DB), open-folder (Step 5), password prompting
(Step 5), encrypted passwords. Decision record: `decisions/005-step3-storage-jobs.md`.
Verified on this PC (Windows 11, Python 3.14): a directory ACL set with `icacls` is inherited by files
created with `os.replace`; `atexit` handlers run while daemon threads are alive and can join them.

### 10.1 Interfaces
| Module | Public API |
|---|---|
| `store.py` | `class StoreError(ValueError)`; `class Store(data_dir: str \| PathLike \| None = None)` (default: env `PGBS_DATA_DIR`, else `<app dir>/data`); attribute `warnings: list[str]`. Servers: `list_servers() -> list[dict]` (public views), `get_server(id, *, with_password=False) -> dict \| None`, `save_server(data) -> dict` (create when no `id`, else update; returns public view), `delete_server(id) -> bool` (also deletes its profiles), `public_server(rec) -> dict` (static). Settings: `get_settings() -> dict` (defaults merged), `save_settings(changes) -> dict`. History: `add_history(entry) -> dict`, `update_history(id, changes) -> dict \| None`, `get_history(id)`, `list_history() -> list[dict]` (newest first), `delete_history(id, *, delete_file=False) -> dict \| None`, `mark_interrupted() -> list[dict]`. Profiles: `get_profile(server_id, db) -> dict \| None`, `save_profile(server_id, db, profile) -> dict`, `delete_profile(server_id, db) -> bool`. Module: `default_store() -> Store` (lazy singleton) |
| `jobs.py` | `class JobError(Exception)` (`.code`: `not_found` \| `invalid` \| `conflict` \| `busy`); `class JobManager(store, *, backup_fn=backup.run_backup, restore_fn=restore.run_restore, keep_finished=50, max_running=4)`; `start_backup(server_id, dbname, options=None, *, password=None) -> str`; `start_restore(server_id, dbname, path, options=None, *, password=None, source_history_id=None) -> str`; `get_job(id, log_after: int \| None = None) -> dict \| None`; `list_jobs() -> list[dict]` (newest first, no log); `cancel_job(id) -> bool`; `shutdown(timeout=15.0)`; `recover_interrupted() -> int` (called by `__init__`). Module: `init(store) -> JobManager`, `get_manager()` |
| `engine/backup.py` | `_target_path` reserves the `.part` with `open(part, "xb")` (§10.7); new event `file {path, part}` before writing; `object` and `table_done` gain `bytes_written` |
| `engine/precheck.py` | `order_index_checks(conn, pairs: list[tuple[int, str]]) -> dict[tuple[int, str], dict]` (batched); `order_index_check` becomes a wrapper; `Thresholds.from_settings` accepts any value |

`backup_fn`/`restore_fn` are injectable so QA can drive the tracker with scripted events (no DB).

### 10.2 File formats (UTF-8, `indent=2`, `ensure_ascii=False`, top-level `version`)
`servers.json`
```json
{"version": 1, "servers": [{"id": "9f2c41d0a7b3", "name": "Local 18", "host": "localhost", "port": 5432,
  "user": "postgres", "password": "secret", "remember_password": true, "sslmode": "prefer",
  "maintenance_db": null, "created_at": "2026-10-26T14:03:00+06:00", "updated_at": "2026-10-26T14:03:00+06:00"}]}
```
`settings.json` (only these keys; unknown keys rejected on save, ignored on load)
```json
{"version": 1, "settings": {"output_dir": "~/pg_backups", "large_table_mb": 500,
  "large_table_rows": 1000000, "history_limit": 1000}}
```
`history.json` (oldest first on disk; `list_history` returns newest first)
```json
{"version": 1, "entries": [{"id": "5be1c0f2a913", "kind": "backup", "status": "success",
  "server": {"id": "9f2c41d0a7b3", "name": "Local 18", "host": "localhost", "port": 5432},
  "database": "shop", "file": "C:\\Users\\me\\pg_backups\\shop_20261026_140301_partial.sql.gz",
  "part_file": null, "size": 7340032, "started": "2026-10-26T14:03:01+06:00", "duration_s": 42.7,
  "mode": "full", "gzip": true, "objects": 310, "tables": 12, "rows": 120000, "error_count": 0,
  "warning_count": 1, "error": null, "partial": true,
  "partial_tables": [{"table": "\"sales data\".orders", "data": "latest", "order_by": "ordered_at", "limit": 1000}],
  "errors": []}]}
```
`backup_profiles.json`: exactly §9.8 (value = `BackupOptions.to_profile()`).
- Restore entries: `file` = source file, `database` = target, `source_history_id`, `options`
  (`on_error`, `single_transaction`), `objects`, `rows`, `error_count`, `warning_count`, `error` (first
  error message), `errors` = first 20 `{object, sqlstate, message, line}`; `partial` = false.
- `status`: `running` (written at start) → `success` \| `completed_with_errors` (restore) \| `failed` \|
  `cancelled` \| `interrupted` (app ended while running; set at next start).
- History entry `id` = job id. Ids: `secrets.token_hex(6)`, re-drawn on collision.
- Times: local ISO-8601 with offset, seconds precision (same as `to_profile`).

### 10.3 Store rules
| Topic | Rule |
|---|---|
| Lock | One `threading.RLock` per `Store`; every read-modify-write is `with lock: read file → change → write`. No in-memory cache (always re-read; files are small), so two `Store` objects on one dir stay consistent |
| Atomic write | `os.open(dir/.<name>.<pid>.<tid>.tmp, O_WRONLY\|O_CREAT\|O_EXCL\|O_BINARY, 0o600)` → write → `flush` + `os.fsync` → `os.replace(tmp, final)`; on Windows retry `PermissionError` 10× every 50 ms (antivirus/indexer holding the file); tmp removed on any failure. Start-up deletes stale `.*.tmp` in the data dir |
| Missing file | Defaults (empty lists / default settings); file created on first write. `data/` created on first `Store()` |
| Corrupt file | Bad JSON, non-UTF-8, top level not an object, or wrong container type → rename to `<name>.corrupt-YYYYMMDD-HHMMSS.json`, continue empty, append to `store.warnings` + `log.warning` ("servers.json was unreadable; saved as … and started empty"). Never raises to the caller. Bad *items* (e.g. a server without `id`) are skipped with a warning; file kept |
| Version | `CURRENT = 1` per file; `MIGRATIONS: {from_version: fn}` applied on read, saved on next write. `version` > CURRENT → read best-effort, **refuse writes** (`StoreError("… was written by a newer PG Backup Studio")`) |
| Server validation | `name` non-empty, unique (case-insensitive); `host` non-empty; `port` int 1–65535; `user` non-empty; `sslmode` in disable/allow/prefer/require/verify-ca/verify-full (default prefer); `maintenance_db` str or null. Errors → `StoreError` with a user message |
| Password | Update: key absent or `None` → keep stored; string → replace; `remember_password=false` → stored password set to `""`. `public_server` = record without `password` + `has_password: bool(password)`. Only `get_server(with_password=True)` (used by jobs) returns it |
| Permissions | POSIX: `data/` `chmod 0700`, files created 0600 (`os.open` mode). Windows: decision 005 §2 (recommended: best-effort `icacls data /inheritance:r /grant:r <DOMAIN\user>:(OI)(CI)F *S-1-5-18:(OI)(CI)F` once per start; failure → warning only) |
| Settings | `get_settings` merges defaults; invalid stored values → default + warning. `save_settings(changes)`: `output_dir` non-empty str (stored unexpanded); thresholds finite > 0; `history_limit` int 50–100000 |
| History | `add_history` appends, then trims the oldest **finished** entries beyond `history_limit` (never `running`). `delete_history(delete_file=True)` deletes only when `kind=backup`, the path ends `.sql`/`.sql.gz` and is a file; a missing file is not an error (entry returned with `file_deleted: false`) |
| Profiles | Shape check only (`saved_at` str, `options` dict); jobs saves only validated options |

### 10.4 Job model and lifecycle
`Job` (internal): `id, kind, status, phase, cancel_event, lock (threading.Lock), started_wall, t0 (monotonic),
t_work (monotonic at the 'write'/'restore' phase), counters, current, log deque(maxlen=200), log_seq,
error_count, warning_count, result, error, file, part_file, history_error, _server (with password, never
exposed), _max_pct`.
1. `start_*` (caller thread): look up server (`JobError not_found`), apply `password` override (memory only),
   validate (`BackupOptions.from_value` / `RestoreOptions.from_value`, file exists) → `JobError invalid`;
   `max_running` reached → `busy`; a running restore into the same server + DB → `conflict`. Create the Job
   (`status=running, phase=starting`), register it, `store.add_history({..., status: running})`, start
   `threading.Thread(daemon=True, name="pgbs-job-<id>")`, return the id.
2. Worker: `phase=connecting`; `backup_fn(server, db, opts, progress=tracker, cancel=event)`. The tracker
   updates fields under `job.lock` (fast, never raises, never calls the store). The `file` event's `part`
   is written to the history entry once (`update_history`) so a crash can be cleaned up.
3. End: map the outcome (table below) → `store.update_history(id, final fields)`; backup success →
   `store.save_profile(server_id, db, opts.to_profile())` and `save_settings({"output_dir": opts.out_dir})`.
   Store errors → `history_error` + log warning (job outcome unchanged). **Then** set the terminal status
   under the lock, so a poll that sees the end status can rely on history/profile being saved.
4. Registry keeps every running job + the newest `keep_finished` finished ones (pruned on each start).

| Engine outcome | Job status | `error` |
|---|---|---|
| backup returns | `success` (`partial` in result) | null |
| `Cancelled` | `cancelled` | "Cancelled by user" |
| `BackupBlockedError` | `failed` (`result.problems` = `.problems`) | message |
| `LockTimeoutError` / `EngineError` | `failed` | `str(e)` |
| any other exception | `failed` (+ `log.exception`) | "Unexpected error: <type>: <msg>" |
| restore `status` ok / completed_with_errors / failed / cancelled | same (ok → `success`) | first error message when failed |

**Cancel:** `cancel_job` sets the event if running (False if unknown/finished), `cancel_requested=true`,
`phase=cancelling`; the engine (via `cancel_on_event`) stops within ~1 s and removes `.part`.
**App close:** `atexit.register(manager.shutdown)`: sets every cancel event, joins up to 15 s; history becomes
`cancelled` with "Cancelled: the app was closed". **Hard kill** (console window closed, power loss): at next
start `recover_interrupted()` → `store.mark_interrupted()` turns every `running` entry into `interrupted` and
deletes its recorded `part_file` if it exists and ends with `.part` (never globbed).

### 10.5 Snapshot (what `GET /api/jobs/<id>` will return in Step 5)
Built under `job.lock` as a new plain dict (lists copied), so pollers never see a half-updated job or share
mutable state; derived fields are computed at snapshot time. `log_after=n` returns only lines with `seq > n`.
```json
{"id": "5be1c0f2a913", "kind": "backup", "status": "running", "phase": "write", "cancel_requested": false,
 "server": {"id": "9f2c41d0a7b3", "name": "Local 18"}, "database": "shop", "started": "2026-10-26T14:03:01+06:00",
 "elapsed_s": 12.4, "percent": 43.7, "eta_s": 16, "file": "C:\\...\\shop_20261026_140301.sql",
 "progress": {"objects_done": 120, "objects_total": 310, "tables_done": 4, "tables_total": 12, "rows": 120000,
   "est_rows": 500000, "data_bytes_done": 52428800, "data_bytes_total": 125829120, "bytes_written": 7340032,
   "current": {"name": "\"sales data\".orders", "rows": 40000, "est_rows": 100000}},
 "error_count": 0, "warning_count": 1, "log_seq": 17,
 "log": [{"seq": 17, "t": 11.9, "level": "info", "msg": "Copied app.customers: 5,000 rows", "error": false}],
 "result": null, "error": null, "history_id": "5be1c0f2a913"}
```
Restore `progress`: `{file_size, position, objects_done, rows, current: {kind, name}}`. Log lines: phase
changes, warnings, errors (`error: true`), each `table_done`, the final summary; one line, ≤ 500 chars.
Per-object events are not logged (thousands). `error_count`: restore = engine count; backup = 1 on failure.

### 10.6 Progress formulas (tracker; engine throttles to `PROGRESS_INTERVAL` 0.25 s)
**Backup.** Phases `starting → connecting → catalog → write → saving → done`. Until `totals`: 0 %, ETA null.
- `O` = objects_total, `o` = objects_done; `D` = data_bytes_total (Σ `CopySpec.size`).
- `d = Σ size of finished tables + f·size_cur`, `f = min(bytes_copied / size_cur, 0.99)` if `size_cur > 0`
  else `min(rows / est_rows, 0.99)` (COPY text bytes differ from on-disk size, hence the cap).
- Weights `wo, wd = 0.2, 0.8`; `D = 0` (schema mode / empty tables) → `1, 0`; `O = 0` → `0, 1`.
- `p = wo·o/O + wd·d/D`; `percent = round(min(max(p, _max_pct), 0.999)·100, 1)` (never decreases); 100 only at `success`.

**Restore.** `p = position / file_size` (compressed position for `.gz`; `file_size = 0` → 0); same clamp.

**ETA** (both): `t = now − t_work` (excludes connect/catalog); `eta_s = round(t·(1−p)/p)` when `p ≥ 0.02` and
`t ≥ 2 s`, else null; null once finished.

### 10.7 Engine changes and Step 2 bug fixes
| Item | Fix |
|---|---|
| **Two backups of one DB started in the same second get the same file name** (found in this review; with concurrent jobs both open the same `.part` with `wb` → silently corrupt files) | In `_target_path`'s loop create the `.part` with `open(part, "xb")` (exclusive) and close it; `FileExistsError` → next suffix; an explicit `filename` whose final or `.part` exists → `BackupError`. `_Output` then reopens it `wb` |
| Jobs need the part/final path and the size after the last COPY | Emit `file {path, part}`; add `bytes_written` to `object` / `table_done` |
| BUG-S2-3 | `run_check`: group leaves once (`by_root = defaultdict(list)`). New `order_index_checks`: one query for columns (`unnest(%s::oid[], %s::text[])` joined to `pg_attribute`, PG 18 NOT-NULL-NOT-VALID gate kept) and one for indexes (`indrelid = ANY(...)`, `indkey[0]`, `indoption[0]`, btree, `indpred IS NULL`, `indisvalid`, `ORDER BY relname`); same decision rules in Python. `_table_entry` reads results from a dict. All columns used exist in PG 12 |
| BUG-S2-4 | `_lock_snapshot`: catch `(errors.UndefinedTable, errors.InvalidSchemaName)`; message "Tables or schemas were dropped or renamed while the backup was starting; try again." (With an explicit schema list the retry's `resolve_schemas` reports the missing schema by name.) |
| BUG-S2-5 | `from_settings`: `d = settings if isinstance(settings, Mapping) else {}`; also reject non-finite numbers (`math.isfinite`) |

### 10.8 Risks and edge cases
| Risk | Handling |
|---|---|
| Two app processes on one `data/` (different `PORT`) | Lost update possible; no cache keeps the window tiny; README: run one instance |
| Disk full / read-only data dir | `OSError` on write → job keeps its outcome, `history_error` set; tmp removed; old file intact |
| Ctrl+C during a long backup | Cancelled within the 15 s grace; history `cancelled` |
| Console window closed (≈5 s before Windows kills the process) | Cleanup may not finish → next start marks `interrupted` and deletes the recorded `.part` |
| Polling while the tracker updates | Per-job lock; the snapshot is a copy; tracker work is O(1) per event |
| Password leak via job/history/log | `_server` private; snapshots/history keep `server {id, name, host, port}` only; libpq errors carry no password |
| Huge history | Trimmed to `history_limit`; restore `errors` capped at 20 per entry |
| Restore into a DB another job is restoring | `JobError conflict` |
| Server deleted while its job runs | Job keeps its in-memory copy; history keeps the name snapshot |

### 10.9 Test plan (QA; `Store(tmp_path)`, `JobManager(store, backup_fn=fake)` for unit tests, real PG 18 for the rest)
1. **Files survive restart:** save server/settings/profile/history with `Store(d)`; a new `Store(d)` returns
   identical data; `list_servers()` / `public_server` never contain `password`; `has_password` correct; password
   keep/replace/clear rules; validation error per field; duplicate name.
2. **Atomic + concurrent:** patch `os.replace` to raise → old file intact, no tmp left; 20 threads × 50
   `add_history` → 1000 entries, valid JSON; `PermissionError` on the first 3 replaces → succeeds.
3. **Corrupt / versions:** garbage bytes, `[]`, `{"version": 1, "servers": {}}`, Latin-1 bytes → `*.corrupt-*.json`
   exists, empty data, `warnings` non-empty; `version: 99` → reads, write raises `StoreError`.
4. **Settings / profiles:** defaults; invalid values fall back; unknown key rejected; `delete_server` removes its
   profiles; `to_profile()` round trip → `BackupOptions.from_dict` equal; history trim keeps `running` entries.
5. **Permissions:** POSIX 0600/0700 (skip on Windows); Windows (if chosen) `icacls data` lists only the user and
   SYSTEM, and a freshly written `servers.json` inherits that.
6. **Progress 0→100** (real backup of `pgbs_test_src`, speed limit 1 MB/s), polled every 50 ms: percent never
   decreases, 0 during `catalog`, < 100 while running, 100 at `success`; phases in order; final `rows` = result
   rows; `tables_done = tables_total`; ETA appears and is null at the end; a schema-only backup reaches 100.
   Restore of that file: percent from position, 100 at the end, `rows` = result rows.
7. **Formula unit tests (fake engine):** scripted events → exact percents for the weighting, the 0.99 cap,
   `D = 0`, `O = 0`, restore `file_size = 0`.
8. **Cancel cleanup:** cancel mid-COPY → `cancelled` within 2 s, no `.part`, no final file, history `cancelled`,
   no profile saved, no `PG Backup Studio` backend left; a second cancel returns False.
9. **History / profile at the end:** success → entry fields per §10.2 (partial backup: `partial` + table list),
   profile saved with `saved_at`, `settings.output_dir` updated; failure (lock timeout) → `failed`, error text,
   no profile; restore with an injected bad statement → `completed_with_errors`, `errors` ≤ 20.
10. **Concurrency:** 3 backups of the same DB started together → 3 distinct, complete files; 5 pollers × 500
    snapshots during a job → no exception; mutating a snapshot does not change the job; `max_running` → `busy`.
11. **Restart:** history entry `running` + a real `x.sql.part` → a new `JobManager` marks it `interrupted` and
    deletes only that file; `shutdown()` during a running backup → `cancelled`, no `.part`.
12. **Bug fixes:** the three Step 2 xfail-strict tests pass (markers removed); `order_index_checks` equals
    `order_index_check` for every fixture candidate column; 18k-table pre-check scales linearly.
