# 004 — Step 2: locking, blocked pre-checks, partial-backup FKs, "latest N" query, profiles, MAINTAIN

_Date: 2026-10-25 · Status: proposed (needs user approval with the Step 2 plan). Verified on PG 18.4._

## 1. Lock every readable table up front, before the snapshot (like pg_dump)
**Context.** Verified: after a REPEATABLE READ snapshot, a concurrent `TRUNCATE` or table rewrite
(`ALTER COLUMN TYPE`) makes `COPY` return 0 rows with no error, so the backup is silently incomplete.
Catalog readers (`pg_table_size`, `pg_get_expr`) also wait briefly for each table's lock, so today a
locked table makes the backup hang (or fail with no table name) while "reading the catalog".
**Decision.** In the backup transaction, before the first query that takes the snapshot (`SET`/`LOCK`
take none, verified), run `LOCK TABLE ONLY t IN ACCESS SHARE MODE` per table of the selected schemas that
the user can `SELECT`, under `lock_timeout`. A timeout names the table; nothing has been written yet.
**Cost.** ACCESS SHARE is the lock reading takes anyway, but it is now held from the start: DDL on those
tables waits until the backup ends (as with pg_dump). Rejected: locking each table only at its COPY
(keeps the silent-empty-table risk).

## 2. FK `NOT VALID` only where it is needed
**Context.** The spec says "every FK that references or belongs to" a partial/schema-only table. An FK
whose own table is partial but whose referenced table is full is always satisfied (a subset of rows from
one consistent snapshot). On PG 12–17 `NOT VALID` is not allowed on FKs of partitioned tables (added in
PG 18), so the spec rule would make restores of e.g. a "latest N" partitioned `orders` (FK to customers) fail.
**Decision.** `NOT VALID` when the referenced table is not Full and the referencing table has data (is
not Schema only). Already-NOT-VALID and PG 18 `NOT ENFORCED` FKs are left alone. If the referencing table
is partitioned and the source is older than PG 18, the FK is left out with a warning and its exact
`ALTER TABLE ... ADD CONSTRAINT` is written in the header. **Deviates from the spec wording — user approval needed.**

## 3. A blocked pre-check stops the backup; the fix is explicit
`run_backup` repeats the permission check and raises `BackupBlockedError` (list of problems). No
"ignore and continue" option. The user either unticks the schema or sets the table to "Schema only"
(its structure is still backed up; the backup is marked Partial). Rationale: never a silently filtered
(RLS) or missing table.

## 4. "Latest N" query form
Verified: `ORDER BY c DESC NULLS LAST LIMIT n` never uses a normal (ASC) index, even on a NOT NULL
column — it sorts the whole table; `ORDER BY c DESC` uses the index. When the column cannot hold NULLs
(`attnotnull`, and on PG 18 the NOT NULL constraint is validated) the backup uses `DESC` (same rows).
For nullable columns it keeps `DESC NULLS LAST`; the pre-check says whether an index can serve it
(`DESC NULLS LAST` or `ASC NULLS FIRST` index) and suggests one, never creates it.

## 5. MAINTAIN grants (open; lead recommends Option A)
Option A: when a grantee holds the full privilege set for the object kind on the source version, emit
`GRANT ALL` instead of listing privileges, so PG 17/18 backups restore owner grants on PG 12–16.
Option B: document only. Implementation of A is ~15 lines in `backup._acl` (ARCHITECTURE §9.9).

## 6. Profiles: shape now, file I/O in Step 3
The engine defines `BackupOptions.to_dict()` / `from_dict()` and `reconcile_table_options()`; the
`backup_profiles.json` shape is fixed in ARCHITECTURE §9.8. Reading/writing the file (atomic write, lock)
is done once, in `store.py` (Step 3), instead of a throwaway helper now. The spec's "next backup opens
pre-filled" acceptance item is therefore verified in Step 3; Step 2 QA verifies the round trip and reconcile.

## 7. Small conventions
Speed limit in MiB/s of uncompressed COPY data, minimum 0.1. Lock timeout seconds, default 10, 0 = wait
forever. Pre-check uses its own short autocommit connection with a 2 s lock wait and falls back to
lock-free size estimates for locked tables. Cancel uses a watcher thread calling `conn.cancel_safe()`
(verified to end a lock wait at once); also applied to restore.
