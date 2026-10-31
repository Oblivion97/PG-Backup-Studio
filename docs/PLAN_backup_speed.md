# Plan — faster, self-adjusting backups

_Date: 2026-10-29 · Status: **draft, not approved, nothing built.** To be picked up later. When approved, record the
choices as `docs/decisions/011-backup-speed.md` and the design as an ARCHITECTURE section._

## Goal
Backups as fast as the PC, the network and the database allow, while:
- the PC that runs the backup stays responsive,
- the database server is not slowed down for its other users,
- the backup never uses up the server's connection slots and locks users out.

## How a backup runs today (`engine/backup.py`)
- One connection, one thread, one REPEATABLE READ snapshot; tables are read one after another with
  `COPY … TO STDOUT` (text) into one plain `.sql` file.
- The same thread reads from the network, gzips (level 6, when on) and writes the file, so these steps wait on each
  other instead of overlapping.
- psycopg hands over COPY data about one row at a time; per-row Python work adds up on tables with millions of rows.
- Writes are batched at 256 KB (`WRITE_BATCH_BYTES`); progress events every 0.25 s.
- Optional fixed speed limit (`_Throttle`, MiB/s of uncompressed COPY data).
- Connections used by the app today: 1 per backup (+ a short pre-check connection), up to 4 jobs at once
  (`jobs.py`), up to 16 query-tab sessions (`engine/console.py`). All carry the app's `application_name`.

## Step 0 — measure first (no code)
Run a backup of a large database and watch Task Manager:
- App at about one full core (100 % ÷ number of cores) → **CPU-bound**: compression and per-row work (steps 2, 3).
- CPU low, network busy and steady → **network or server bound**: only parallel reading or running closer to the
  server helps (step 5).

Also back up the same database once with gzip on and once off; a large difference means compression is the cost.

### Settings that help today (no code)
| Change | Why |
|---|---|
| Leave **Speed limit** empty | Any value makes the backup sleep on purpose. |
| **gzip off** when disk space allows | Level-6 gzip in the same thread is usually the biggest CPU cost. |
| Big log / audit / history tables → **Schema only** or **Latest N** | Rows not copied cost nothing; often the biggest win. |
| Index the ordering column for **Latest N** | Without one the server sorts the whole table (the pre-check shows it). |
| Save to a local SSD, not a network share | Avoids a second network trip. |
| Exclude the backup folder from Defender real-time scanning | Scanning slows writing large files. |
| Run the app close to the database | Bandwidth to a remote server is often the real ceiling. |
| `sslmode=disable` on a trusted internal network | Saves encryption CPU; traffic is no longer encrypted — only if policy allows. |

## Step 1 — background priority for the backup thread (small)
- On Windows, the job thread calls `SetThreadPriority(GetCurrentThread(), THREAD_MODE_BACKGROUND_BEGIN)` (ctypes, no
  new dependency): low CPU **and** low disk-I/O priority. Whatever the user does on the PC comes first; the backup
  still uses everything idle. Only the backup / restore thread, not the window or the API.
- Ends with `THREAD_MODE_BACKGROUND_END` when the job finishes (threads are reused by nothing else, but be tidy).
- No effect on other OSes (no-op).

## Step 2 — overlap reading, compressing and writing (small)
- A writer thread fed through a bounded queue (e.g. 8 × 1 MB) does gzip + file writes; zlib and file I/O release the
  GIL, so network reads and compression run at the same time. Bounded queue = memory stays small.
- Write batch 256 KB → 1 MB (fewer system calls and progress events).
- Errors in the writer thread are passed back and end the backup as today; Cancel stops both threads.

## Step 3 — compression level setting (small)
- gzip level as a setting: **Fast** (1), **Balanced** (6, today), **Small** (9). Level 1 is several times faster,
  file somewhat larger.
- Auto (step 4) may switch to Fast when the CPU is the limit.

## Step 4 — "Auto" speed (medium)
A controller adjusts the existing `_Throttle` rate while the backup runs. **It opens no extra connection.**

**Signals (every 1–2 s):**
| Signal | Measured by | Meaning |
|---|---|---|
| COPY throughput | MB/s read from the server | Rising rate no longer raises MB/s → network or server full; stop pushing. |
| COPY stalls | time waiting for the next chunk vs. the start-of-backup baseline | Server or network getting busy. |
| PC CPU | Windows `GetSystemTimes` via ctypes | Other programs need the CPU. |
| PC disk | time per file write | Disk saturated. |

The database server's own CPU is not visible to a normal login; COPY stalls are the proxy.

**Control (AIMD, like TCP):** all healthy → raise the rate about 10 % every few seconds; any signal bad → cut 30–50 %
at once. A floor (e.g. 0.5 MB/s) so a backup always finishes.

**Modes (Settings + Backup form):** **Auto** (recommended) · **Fast** (no limits) · **Gentle** (Auto with lower targets)
· **Fixed MB/s** (today's option).

**Shown to the user:** the running job card shows the current speed and why ("12 MB/s · slowed: database busy",
"38 MB/s · full speed"); every change is written to the job log.

## Step 5 — parallel workers (large, opt-in, later)
Same idea as `pg_dump -j`:
- The main connection exports its snapshot (`pg_export_snapshot()`); each worker connection runs
  `SET TRANSACTION SNAPSHOT '<id>'` in REPEATABLE READ READ ONLY → the backup stays exactly consistent.
- Workers copy different tables into temporary part files; at the end the parts are joined in plan order into the
  usual single `.sql` file (one extra local copy), so restore is unchanged.
- One huge table can be split across workers by primary-key ranges, or `ctid` ranges on PostgreSQL 14+.
- Expected gain with 4 workers on a database with several big tables: about 2–4× when the network and server have
  spare capacity.
- Costs: more load on the server, temporary disk space for the parts, one more setting.
- Auto (step 4) decides how many workers to run, within the connection budget below.

### Connection budget (must hold before step 5 ships)
Extra connections can fill the server and lock other users out. Limits that matter: `max_connections` (minus
`superuser_reserved_connections` and, on PG 16+, `reserved_connections`), the database's `CONNECTION LIMIT`, the
login's `CONNECTION LIMIT` (most dangerous when an application shares the login), and a pooler such as PgBouncer.

Rules:
1. **Check first.** Read the limits and count sessions in `pg_stat_activity` (any login can count them).
2. **Only real spare capacity.** Open extra workers only if afterwards at least max(20 % of `max_connections`, 10)
   slots stay free, and the same margin holds for the role and database limits. Otherwise one connection, as today,
   with a log line ("1 worker: only 6 connections free").
3. **Hard cap:** at most 4 workers per backup; a setting "Parallel workers" (0 = always one connection, the
   default).
4. **Never fight for a slot.** A refused connection ("too many connections") is not retried; the backup carries on
   with what it has.
5. **Give slots back.** If free slots drop below the margin during the backup, a worker closes after its current
   table.
6. **PgBouncer in transaction mode** cannot share a snapshot: detect it and fall back to one connection.
7. **Whole-app budget.** Query-tab sessions and other jobs count against the same budget.

| Situation | Workers |
|---|---|
| Mostly idle server, 100 slots, 15 in use | up to 4 |
| Busy server, 92 of 100 in use | 1 (today's behaviour) |
| Login limited to 5 connections, app already uses 3 | 1 |
| Server fills up during the backup | workers close one by one, down to 1 |

## Not planned
- **Binary COPY / custom file format**: faster for some types, but the backup would no longer be a plain `.sql` file
  any PostgreSQL tool can restore — a core point of the app.

## Known trade-offs
- Auto reacts; a sudden server spike sees a few seconds of extra load before the backup backs off.
- A slower backup holds its snapshot longer, which delays vacuum cleanup a little on a busy server (true of any slow
  backup, including today's fixed limit).
- Locks are unchanged: ACCESS SHARE on every table for the whole backup; reads and writes are never blocked, only
  schema changes (`ALTER TABLE`, …) wait.

## Before starting
- `static/index.html` has about 0.5 KB left under its 150 KiB limit: move the backup screen into its own script
  (like the SQL workspace files) before adding the speed controls.
- Decide: is Auto the default for new backups? Default parallel workers 0 (opt-in)?

## Tests (outline)
- No database: AIMD controller with fake signals (ramps up, cuts down, floor, Fixed / Fast modes); budget rules
  from fake limit numbers (margin, role limit, refused connection); writer thread error and cancel paths;
  background-priority call is a no-op off Windows.
- PostgreSQL (`pgbs_test_*`): Auto backup round-trips identical to a normal backup; gzip levels round-trip; parallel
  backup output restores to the same data as a single-connection backup; workers share one snapshot (rows inserted
  mid-backup are absent in every part); a login with `CONNECTION LIMIT 2` gets one worker and still succeeds.

## Order
1. Background priority (step 1) — tiny, immediate benefit on the PC.
2. Writer thread + compression setting (steps 2–3).
3. Auto speed (step 4), with the UI and job-card status.
4. Parallel workers with the connection budget (step 5) — opt-in, later.
