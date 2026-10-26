# QA Report

## 2026-10-25 - Step 0 (project setup and test foundation)

**Verdict: PASS**

Environment: PostgreSQL 18.4 (localhost:5432), Python 3.14.6, Flask 3.1.3, psycopg 3.3.6, pytest 9.1.1.

Cases run: 64, passed 64, failed 0 (about 2 s).
- 10 module imports (app, jobs, store, engine, engine.util/db/precheck/catalog/backup/restore) + `Cancelled` is an Exception.
- Flask test client: `/` returns 200, `/x` returns 404; `app.py` binds 127.0.0.1 only.
- Fixture `tests/fixtures/sample_db.sql` loads with exit 0 and empty stderr under `ON_ERROR_STOP`; DB is UTF8.
- 42 catalog-count checks, one per acceptance-test item (extension, schemas incl. one with a space, enum/domain/composite/range,
  sequence options, identity ALWAYS/BY DEFAULT, serial, generated, COLLATE "C", partitions + DEFAULT, 5000 orders rows,
  inheritance, UNLOGGED + fillfactor, self FK, FK into partitioned table, plpgsql/SQL/row-type/BEGIN ATOMIC functions,
  procedure, aggregate INITCOND, view-on-view, matview + unique index, enabled/disabled trigger, rule, RLS + policy,
  quoted comments, grants, empty and zero-column tables, reserved/spaced identifiers).
- Edge data (tab, newline, CRLF, backslash, `\N`, NULL vs 'NULL', bytea with \x00, unicode) and deterministic values.
- Guard: `drop_database` refuses names not matching `pgbs_test_*`; `restore_db` factory naming.
- Wrong password: a subprocess pytest run skips the DB tests with "PostgreSQL not reachable" (no errors).
- Cleanup verified: no `pgbs_test_*` databases or `pgbs_test_reader` role remain after the run.

Bugs: none found in production code (all modules are stubs at this step).

Notes for later steps:
- `pgbs_test_reader` is a cluster-level role; restores that include GRANTs need it to exist (the role is dropped at session end).
- Fixture orders span partitions 2024q1, 2024q2 and the DEFAULT partition; `ordered_at` is the "latest N" column.
- `app.secrets` has RLS enabled with a policy; the superuser bypasses it, a read-only user does not (pre-check test target).

Rerun: `$env:PGPASSWORD='<password>'; python -m pytest -q`

## 2026-10-25 - Step 1 (backup + restore engine)

**Verdict: FAIL** (BUG-1 blocker and BUG-2 major are open; everything else in scope passes).

Environment: PostgreSQL 18.4 only (localhost:5432; no other major installed, so PG 12-17 behaviour is untested), Python 3.14.6, psycopg 3.3.6.
Whole suite: 214 tests = 207 passed, 4 failed (BUG-1 x2, BUG-2 x2), 3 expected-fail (BUG-3, BUG-4, OBS-1). My new tests: 107
(`tests/test_step1_roundtrip.py`, `tests/test_step1_restore.py`, `tests/test_step1_env.py`, helper `tests/step1_helpers.py`,
extra fixtures `tests/fixtures/exotic.sql` and `tests/fixtures/weird_idents.sql`). Scratch DBs and roles are `pgbs_test_qa_*`;
verified none remain after the run. The password is only read from PGPASSWORD (test roles get a random runtime password).

### Results per acceptance criterion
1. Full round trip, plain and gzip: PASS. 0 errors / 0 warnings; `pg_dump -s` identical (comments, `\restrict`, blanks stripped, sorted);
   row counts of all tables; md5 of orders / edge / customers; sequences (last_value, is_called) and `nextval()`; matview populated and equal;
   trigger states equal (incl. disabled); functions, views, rule, trigger, policy, aggregate, BEGIN ATOMIC work after restore;
   no `\r\n` in the file; every object is preceded by a `-- Type: X; Name: Y` label (label count = objects + tables); header matches the spec;
   gzip and plain bodies are identical. Extra fixture `exotic.sql` (list/hash/sub-partitions, cross-schema partitions and inheritance,
   multi-parent inheritance, deferrable and NOT VALID FKs, domain on domain, PG18 virtual columns, named NOT NULL, NOT ENFORCED, temporal PK,
   NULLS NOT DISTINCT, INSTEAD OF / constraint / WHEN triggers in every enable state, rules, security_barrier and check-option views, matviews, RLS
   restrictive/force, column/sequence/type/domain/procedure grants, a 30-column datatype table) round-trips with `pg_dump -s` diff 0 and equal data.
2. Schema-only: PASS (structure identical, all tables empty, matview created unpopulated, no COPY in the file).
3. Data-only after schema-only: PASS (counts, md5, sequences equal; audit.log not inflated; disabled trigger still `D`; matview refreshed). Also on exotic.
4. Privileges off: PASS (no GRANT / REVOKE / OWNER TO; no explicit ACLs after restore).
5. Schema filter `app`: PASS with documented warnings. No "sales data" objects, extensions present. Restore has 4 errors, all caused by
   `audit.log_customer` / `audit.log` not being selected; the backup warns about each (3 warnings). `app`+`audit` restores with 0 errors.
   `sales data` alone warns about FKs into `app`.
6. Error modes: PASS (continue: 1 error and the rest identical to source; stop: halts at the statement; single: target empty). Entries carry object label,
   sqlstate, message, line and sql cut to 160 chars. A failing COPY block skips only its own rows.
7. Missing role: PASS in continue / stop / single for GRANT, REVOKE and OWNER TO on schema, table, column, view, sequence, function, type
   (warnings only, `missing_roles` listed, object stays owned by the restoring user). `CREATE POLICY ... TO missing_role` is an error in all 3 modes
   (single: target empty). Quoted odd role names are parsed correctly.
8. Cancel: PASS. Backup cancelled before start, at catalog, mid-objects and mid-COPY of a 300k-row table: `Cancelled`, no `.part`, no final file,
   connection closed. Restore cancelled mid-COPY (plain and single transaction), mid-objects, before start, gzip: status `cancelled`;
   single transaction leaves the target empty; no leftover backends. Cancel during one long statement (`pg_sleep(6)`) waits for it (by spec: checked between statements).
9. Robustness: PARTIAL. Truncated gzip (several cut points, header and trailer), CRC-damaged gzip, gzip-magic garbage, non-UTF-8 (plain and
   inside gzip), truncated plain files (mid-COPY, no end marker, open quote, open dollar quote), gzip by magic when named `.sql`, plain named `.sql.gz`:
   clear errors. FAIL: BUG-1 (newline in table/schema name), BUG-2 (zlib.error). Identifiers with spaces, quotes, `;`, `$`, `--`, `/* */`,
   reserved words, newlines in column / constraint / trigger / policy / view / function names, and hostile literals and comments round-trip (schema diff 0,
   md5 equal) when no table or sequence name contains a newline. LATIN1 source -> UTF8 target and UTF8 -> LATIN1 target work; unrepresentable characters fail loudly.
   CRLF-converted files still restore correctly.
10. No writes: PASS (`tup_inserted/updated/deleted` and `temp_files` unchanged across plain, gzip, schema, data and no-privilege backups; the session is
    repeatable read, read-only, row_security off, UTF8, search_path pg_catalog, statement_timeout 0). Concurrent inserts during a backup are not seen.
11. Version gate: PASS (mock server 9.6, 10, 11.x refused with a message naming the version; 12.0+ accepted; connection closed, no file left).
Also covered: a read-only user (`pg_read_all_data`) backs up and restores; an RLS table it cannot bypass gives a clear BackupError naming the
table and leaves no `.part`; missing SELECT gives "permission denied"; disk-full mid-write leaves no `.part`; odd database name; 3 MB single row and
all 256 byte values in bytea; hostile database-level settings (standard_conforming_strings=off, statement_timeout=1ms, datestyle, search_path,
row_security=on, bytea_output=escape, ...) do not change the restore; NOT INCLUDED warnings in result and header for collations, operators, text
search, publications, foreign tables/servers, default privileges, large objects, event triggers.

### Bugs
- **BUG-1 (blocker)** - `engine/backup.py:835` writes `label_name` raw. Labels of `TABLE DATA` (:434) and `SEQUENCE SET` (:440) are not made single-line
  (`_add` at :217 does it only for DDL nodes). A table or sequence whose qualified name contains a newline gets a label whose second half lands on a new
  line outside the comment. On restore the COPY statement is cut up, 5 syntax errors are reported, the table's rows are NOT loaded, and its COPY data
  lines are then executed as SQL (data content is run as statements).
  Repro: create table "a<newline>b" with a row, back up, restore. Tests: `test_hostile_identifiers_with_newline_in_table_and_sequence_names`,
  `test_every_label_is_a_single_comment_line`. Expected: label collapsed to one line (like other kinds) and the data restored.
- **BUG-2 (major)** - `engine/restore.py:73` `_readline` catches `EOFError, OSError, BadGzipFile` but not `zlib.error`. A gzip file with a damaged deflate
  stream (3 of 40 random single-byte corruptions) makes `run_restore` raise a raw `zlib.error: Error -3 while decompressing data: invalid block type`
  instead of returning `failed` with a "truncated or damaged" message. Repro: `blob = bytearray(gzip.compress(b"SELECT 1;\n" * 2000)); blob[10] = 0xFF`, restore it.
  Tests: `test_gzip_invalid_deflate_data_is_restore_error`, `test_random_bit_flips_never_escape_as_foreign_exception`.
- **BUG-3 (minor)** - `engine/restore.py` `split_sql`: a UTF-8 BOM at the start of the file (editor "save as UTF-8") makes the first statement a syntax error. Loud, not silent. xfail test `test_utf8_bom_file`.
- **BUG-4 (minor)** - `schemas=[]` returns a successful backup with 0 objects (the engine should refuse; the UI must also block "no schema ticked"). xfail test `test_empty_schema_selection_is_refused`.
- **OBS-1 (minor, Step 2/3)** - Cancel is not honoured while the backup waits for a table lock (ACCESS EXCLUSIVE held elsewhere): the thread stays blocked
  until the lock is released (design 8.2 mentions `conn.cancel_safe()`, not implemented; the Step 2 lock_timeout will bound it). xfail test `test_backup_cancel_while_waiting_for_table_lock`.

### Limitations that should be in the README
1. Original grantor, column storage/compression/statistics targets and typed tables are not reproduced.
2. Attached partitions come back in the parent's column order (data verified correct by column name; `pg_dump -s` text may differ for such tables).
3. Owner grants list MAINTAIN (PG 17+): restoring a PG 17/18 backup to PG 12-16 fails those GRANTs right after `REVOKE ALL ... FROM owner`
   (not testable here, only PG 18 installed). Recommend filtering privileges by target version or `GRANT ALL` when the full set is held.
4. Restore as a superuser, or back up with "owners & grants" off: a non-superuser restoring `ALTER ... OWNER TO "postgres"` gets 42501 errors (44 on the fixture).
   Data still restores; with privileges off there are 0 errors.
5. The target must be an empty database (a second restore into a populated database reports errors for the existing objects).
6. NOT INCLUDED kinds: collations, operators/opclasses/opfamilies, casts, text search, conversions, foreign tables/servers, publications, event triggers, default privileges, large objects, base types.
   A schema selection that leaves out schemas used by triggers, rules or FKs gives restore errors (the backup warns).
7. Cancel takes effect between statements/chunks: a long single statement or a lock wait is not interrupted.
8. Backup files must be UTF-8 without BOM.

Rerun: `$env:PGPASSWORD='<password>'; python -m pytest -q` (about 3 minutes). Step 1 only: `python -m pytest -q tests/test_step1_roundtrip.py tests/test_step1_restore.py tests/test_step1_env.py`.

## 2026-10-25 - Step 1 - re-run after fix round 1

Verdict: PASS (Step 1). Full suite `python -m pytest -q`: 216 passed, 0 failed, 1 xfailed (OBS-1, out of scope for Step 2).

Test-side corrections (verified, not taken on trust): `_compare_weird` hard-coded `"new line"."seq;$"` and `"tab le;1"`; the newline test creates `"new<LF>line"` / `"tab<LF>le;1"`. Helper now takes `nl=` and uses the created names. Removed obsolete strict xfails on BUG-3 and BUG-4 tests (both XPASS).

Per-bug status: BUG-1 fixed (label single-line, newline-name round trip ok, sequence (41, True) both sides); BUG-2 fixed (zlib.error -> RestoreError); BUG-3 fixed (BOM skipped; BOM only at file start); BUG-4 fixed (schemas=[] refused, None = all).

New regression tests: matview with newline/double-space names round-trips (plain and gz), REFRESH works, WITH NO DATA state preserved; 180 random gzip corruptions (multi-byte damage, truncation, inserted garbage) raise only RestoreError; BOM + gzip restores, BOM inside a string literal is preserved.

New bugs: none. Scratch DBs/roles left behind: none.
Rerun: `PGPASSWORD=<pw> python -m pytest -q` from the project root.

## 2026-10-26 - Step 2 - Pre-check, safety, partial backups (PostgreSQL 18.4, localhost)

Verdict: **PASS** (no blocker, no major). Full suite `python -m pytest -q`: **380 passed, 0 failed, 3 xfailed** (13.7 min). Baseline before my tests
was 251 passed + 1 xpassed (dev reported 250 + 1). The 3 xfails are strict and document the minor bugs below. I removed the xfail mark of OBS-1
(`test_backup_cancel_while_waiting_for_table_lock`); it passes. About 130 new tests in `tests/test_step2_{safety,partial,precheck,edge}.py`
and `tests/step2_helpers.py`. Only PG 18.4 is installed: PG 12-17 behaviour is simulated (see limitations). No scratch DBs/roles left behind.

### Per-criterion results
| # | Criterion | Result |
|---|---|---|
| 1 | Read-only user | PASS. `pg_read_all_data` login: pre-check blocks with exactly one `rls` problem `app.secrets`; backup raises `BackupBlockedError` naming it, directory empty, no backend left; with `app.secrets` schema-only: partial, restore 0 errors, `pg_dump -s` identical, counts/md5/sequences/triggers/matview identical except secrets = 0. PG 12/13-style role (CONNECT+USAGE+SELECT only) behaves the same. Partial-grant role: every missing schema/table/sequence listed exactly once with the right kind and fixes; column-only grants (all columns) accepted; schema-only fixes tables, a bare sequence can only be fixed by grant or excluding its schema. BYPASSRLS login and RLS table owner read everything; FORCE RLS owner is blocked. `mode=schema` needs no privileges (round trip identical). Full round trip as read-only user without RLS table: identical dump, counts, md5, nextval. |
| 2 | No writes | PASS. tup_inserted/updated/deleted, temp_files, temp_bytes and pg_stat_user_tables n_tup_* unchanged across pre-checks, full/partial/gzip backups, a lock-timeout failure and a cancel. Backup session verified `transaction_read_only=on`, REPEATABLE READ, `row_security=off`; a CREATE TABLE inside it is refused. (`xact_commit` necessarily rises: reads commit; the spec wording should not require it unchanged.) |
| 3 | Lock timeout | PASS. ACCESS EXCLUSIVE on partitioned root, leaf, plain, inherited, zero-column and other tables: fails after 1.5 s (1.3-4.5 s), `LockTimeoutError` names the table, "Nothing was written", no `.part`/final file, no backend left; default is 10 s; 0 waits until the lock is released and then succeeds. Pre-check stays < 5 s, lists the lock (granted) and `size_exact=false`. Only ACCESS EXCLUSIVE blocks (all 7 weaker modes pass). A queued ALTER behind a reader blocks the backup and appears in `locks` with `granted=false`. A lock in an unselected schema is ignored; a schema-only table is still locked first (design). |
| 4 | Up-front lock | PASS. Hooked at the first snapshot query: TRUNCATE, ALTER COLUMN TYPE, DROP, VACUUM FULL, CLUSTER, RENAME, ADD COLUMN DEFAULT random() all fail 55P03 on every table; backup has all rows; source untouched. REINDEX/CREATE INDEX (SHARE locks) and INSERT/UPDATE/DELETE proceed, and the backup does not see the new rows (snapshot). A partition cannot be attached while its table is locked. |
| 5 | Cancel | PASS. Lock wait (timeout 0 and 30 s): stops in < 2 s, no files, no backend left. Throttled COPY (plain and gzip): < 1 s, `.part` removed. Cancel before start / in pre-check. Restore of `pg_sleep(30)`: `cancelled` in < 2 s; single transaction rolls back everything; cancel during a big restore COPY ends cleanly. |
| 6 | Partial backup | PASS. orders latest 1000 + `app.notes` schema-only: name `*_partial.sql`, header lists each table with option / column / N and the relaxed FK, `partial: true` with `partial_tables`, restore 0 errors, orders = the 1000 newest rows (identical to `ORDER BY ordered_at DESC LIMIT 1000`, routed to partitions), notes empty, `order lines` FK `convalidated=false`, `orders_customer_id_fkey` and `customers_referrer_id_fkey` still validated, schema dump identical. Progress `totals.est_rows` = 1000 + other tables (min(N, reltuples)=5000 when N=100000), schema-only tables and partitions never started, phases catalog > write > done. FK matrix (6 option sets incl. self-reference `customers latest 10`) restores with 0 errors and the expected NOT VALID set. Also: gzip name `_partial.sql.gz`, explicit filename kept, ties give exactly N, N > rows, empty table, weird identifiers, nullable column NULLS LAST, sub-partitioned table, inheritance `ONLY`, generated/citext/identity columns, hostile (newline) names keep every header line a comment. "Next backup pre-filled" is Step 3: `to_profile()` has the section 9.8 shape, is JSON-serialisable and round-trips; saved options for vanished tables, partitions, other schemas and corrupt entries are reported in `ignored_options`; new tables default to Full. |
| 7 | Pre-check details | PASS. Defaults 500 MB / 1,000,000; thresholds strictly "greater than"; custom values and both reasons; ordering suggestion (first timestamp-like incl. domain-of-domain, else PK, else none); index advice for 11 index shapes agrees with `EXPLAIN` of the real "latest N" query in every case (NOT NULL / nullable, ASC / DESC / NULLS variants, partial, second column); orders.ordered_at warns, no warning once indexed, an invalid index does not count; partitioned table = one parent with 3 partitions and summed size/rows, partitions never offered; inheritance counted `ONLY`; impact numbers consistent (latest, N > rows, schema-only, schema mode, largest sorted, notes). |
| 8 | GRANT ALL | PASS. Fixture round trip: identical `pg_dump -s` and identical ACLs (aclexplode of relations, schemas, functions, types, columns); file uses `GRANT ALL` where the full set is held (also WITH GRANT OPTION, PUBLIC, sequences, schemas, functions, types, domains, views) and lists privileges when one is missing (e.g. no MAINTAIN); owner with revoked UPDATE/TRIGGER round-trips. |
| 9 | Edge cases | PASS. Table dropped / renamed after the check: retried once; dropped twice: clean `BackupError`, no files; schema-only option for a vanished table ignored with warning. Table created after the check is locked and included; partition attached after the check included; RLS or no-SELECT table created late fails loudly, never silently filtered. 150 sequences + 80 serial tables: exactly the 3 ungranted sequences are reported, all 230 `setval` restore identically. 53200 (about 16.6k tables, default `max_locks_per_transaction`): clear message naming `max_locks_per_transaction`, no files. PG 12-17 FK omission end to end with the version patched to 170000: FK left out, warning, exact `ALTER TABLE ... ADD CONSTRAINT` in the header, restore 0 errors, the header statement runs; a partitioned FK to a Full table is untouched. Option validation: N <= 0 / string / float / bool, missing column or `order_by`, wrong case, bad data value, empty names, speed 0 / 0.09 / negative, lock timeout negative / 3601 / string, unsortable (json) ordering column (clean failure mid-file, `.part` removed), N = 2**62 ok, 2**70 handled. |
| 10 | Regression | PASS. Step 1 suite unchanged and green (251 tests). |

### Speed limit
5 MiB at 1 MB/s (dev), plus mine: 13.6 MiB at 2 and 8 MB/s within 0.85x-1.3x of ideal; 40 small tables (about 100 KB each) at 1 MB/s honoured in total (the clock starts with the first chunk); gzip counts uncompressed bytes. Pre-check: 1500 tables 0.75 s.

### Bugs (all minor; none loses data or breaks a restore)
- **BUG-S2-3 (minor, performance)** - `engine/precheck.py` `run_check`, the `entries = [_table_entry(..., [x for x in leaves if x["root_oid"] == r["oid"]] ...)]` line: grouping leaves per root is O(n^2). Repro: 1500 tables 0.75 s, 4500 tables 5.6 s (linear 2.3 s), 8000 about 10 s, 18,000 tables 143 s. Secondary: `order_index_check` runs 2 queries per candidate column (1000 tables with PK + 2 date columns: 6000 queries, 4.2 s). Fix: group leaves in a dict; batch the index query. Test: `test_precheck_time_grows_linearly_with_table_count` (xfail strict).
- **BUG-S2-4 (minor)** - `engine/backup.py` `_lock_snapshot`: only `UndefinedTable` (42P01) is retried. A schema dropped between check and lock raises 3F000, so the user gets `Backup of X failed: schema "gone" does not exist` (clean, no files, but not retried). Test `test_schema_dropped_after_check_is_retried_like_a_table` (xfail strict).
- **BUG-S2-5 (minor)** - `Thresholds.from_settings("junk")` raises `AttributeError` (non-dict settings content); lists/None are fine. Test xfail strict.

### Observations / limitations to document
- OBS-S2-1: partial ACL sets that include MAINTAIN (for example an owner who lost UPDATE) are written as an explicit list and fail on a PG 12-16 target; only the full set is `GRANT ALL`.
- OBS-S2-2: a schema whose tables are all schema-only is not checked for USAGE (nothing is read from it). A standalone sequence without SELECT has only the `exclude_schema` fix (no per-sequence option).
- OBS-S2-3: the header prints reserved-word column names bare (`latest 4 rows by order`); cosmetic.
- OBS-S2-4: the default ordering column for a composite PK is the first by column order, not the index-leading column (PK (z, a) on columns (a, z) suggests `a`).
- OBS-S2-5: `LOCK` of a parent also locks partitions / children, so the lock error may name a partition or child; a lock on a schema-only table still times out the backup; a column-grants-only table cannot be locked (truncate race remains, README).
- OBS-S2-6: the lock table has slack beyond `max_locks_per_transaction * max_connections` (53200 only at about 14.6k tables on the defaults).
- Not testable here: real PG 12-17 servers (FK omission and MAINTAIN were exercised with a patched `version_num`), progress 0-100 (job layer, Step 3), profile file persistence (Step 3).

Rerun: `PGPASSWORD=<pw> python -m pytest -q` (about 14 min; slow parts: 18k-table fixture about 40 s, 10 s default-timeout test). Step 2 only:
`python -m pytest -q tests/test_dev_step2.py tests/test_step2_safety.py tests/test_step2_partial.py tests/test_step2_precheck.py tests/test_step2_edge.py`.
