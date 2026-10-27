# 006 — Step 4: database management and restore targets

_Date: 2026-10-27 · Status: **approved by the user 2026-10-27** (all six recommendations). Design: ARCHITECTURE §11._

## 1. Protected databases
**Recommended:** refuse drop and rename of `template0`, `template1` and the server's maintenance DB.
Clone and kill stay allowed. Alternative: allow everything behind the typed-name confirmation.
**Why:** dropping `template1` breaks every later `CREATE DATABASE` that uses the default template;
dropping the maintenance DB locks the app out of that server.

## 2. Databases used by a running job
**Recommended:** refuse drop / rename / kill / recreate of a DB while one of this app's jobs uses it
(conflict). Alternative: allow it and let the job fail.
**Why:** killing the backup's own connection gives a confusing failure, and dropping a restore target
mid-restore is never intended.

## 3. Where the restore target is created / recreated
**Recommended:** inside the restore job (new phase "preparing"), so it shows in Activity, the log and
history. Alternative: a separate call before the job starts (simpler, but a drop then happens with no
job record).

## 4. Restoring into an existing database that is not empty
**Recommended:** refuse, unless the user ticks "restore into a non-empty database anyway" (errors such as
"already exists" are then expected). Alternatives: always refuse (use "drop and recreate"), or always allow.
**Why:** the engine assumes an empty target; a silent merge produces many errors and mixed data.

## 5. Kill connections without superuser rights
**Recommended:** terminate what the login may, report `terminated` and `failed` counts, never raise; a
following drop/rename then fails with a clear "N sessions are still connected" message.

## 6. Create defaults
**Recommended:** "Create" uses the server defaults (template1 and its encoding) unless the user picks a
template/encoding; another encoding needs template0 (clear error with that hint otherwise; the UI switches
to template0 automatically in Step 6). Restore "new database" always uses template0 + UTF8 (spec).
