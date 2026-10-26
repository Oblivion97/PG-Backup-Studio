# 005 — Step 3: storage files, job lifecycle, app close, passwords file, concurrency

_Date: 2026-10-26 · Status: proposed (needs user approval with the Step 3 plan). Facts marked "verified"
were checked on Windows 11 / Python 3.14. Design: ARCHITECTURE §10._

## 1. JSON storage: re-read on every change, atomic replace, never crash on a bad file
**Decision.** `Store` keeps no cache: each change is "lock → read file → change → write temp → fsync →
`os.replace`". A corrupt file is renamed `<name>.corrupt-<timestamp>.json` and the app starts that file
empty with a visible warning. Each file has `"version": 1`; a newer version is read but never overwritten.
**Why.** Files are tiny; no cache means no stale data and simple locking. `os.replace` is atomic on NTFS and
POSIX, so a crash leaves either the old or the new file. Windows antivirus/indexers can briefly hold a file,
so `os.replace` retries `PermissionError` for up to 0.5 s.
**Rejected.** SQLite (better, but the spec fixes JSON files); a cache (staleness bugs for no gain).

## 2. Passwords file permissions (plain text, as the user approved)
- POSIX: `data/` 0700, files 0600 (set at creation via `os.open` mode, so no window with wider rights).
- Windows: `os.chmod` cannot restrict readers. Options:
  - **A (recommended):** once per start, best-effort `icacls data /inheritance:r /grant:r <DOMAIN\user>:(OI)(CI)F
    *S-1-5-18:(OI)(CI)F` (only the user and SYSTEM). Verified: files later created by `os.replace` inherit it.
    No new dependency; failure is only a warning. Side effect: local Administrators lose direct access to `data/`.
  - B: rely on the default ACL. Under `C:\Users\<user>\` (where the project is today) only the user, SYSTEM
    and Administrators can read; elsewhere (e.g. `C:\tools`) all signed-in users may read it.
  - C: encrypt with Windows DPAPI: out of scope (spec: "encrypted password storage" later).

## 3. History entry created at start ("running"), completed at the end
**Decision.** Deviates slightly from "history written at the end": the entry is added with status `running`
(and the `.part` path once known), then updated with the outcome. On the next start every `running` entry
becomes `interrupted` and its recorded `.part` file is deleted.
**Why.** If the process is killed (console window closed, power loss) the job leaves no trace and a stray
`.part` file otherwise. Only the recorded path is deleted, never a glob. History trimming never drops
`running` entries.

## 4. Closing the app cancels running jobs
**Decision.** Jobs are in memory (threads), so they cannot survive a restart. On a normal exit (Ctrl+C)
an `atexit` handler cancels every job and waits up to 15 s, so the engine removes `.part` files and
stops server queries; history says "Cancelled: the app was closed". Verified: `atexit` runs while daemon
job threads are still alive and can join them. Rejected: non-daemon threads (Ctrl+C would hang until a
multi-hour backup ends); resuming jobs after restart (a new snapshot would be a different backup).

## 5. History size limit
**Recommended:** keep the newest 1000 entries (`settings.history_limit`, 50–100000). Trimming removes only
the history record, never backup files.

## 6. Unique backup file reservation (bug found in this review)
With concurrent jobs, two backups of the same DB started in the same second compute the same name and both
open the same `.part` with `wb`: both files would be silently corrupt. **Decision:** reserve the `.part`
with exclusive create (`open(part, "xb")`) inside the name loop; a clash moves to the next `_2`, `_3` suffix.

## 7. Progress
Tracker in `jobs.py` (engine sends raw counters, §8.8). Backup: 20 % objects / 80 % data bytes, the current
table counted by `min(bytes_copied / pg_table_size, 0.99)`; restore: compressed file position / size.
Percent never decreases and shows 100 only on success. ETA extrapolates from the start of the write/restore
phase (the catalog read has no measurable progress) and is hidden until 2 % and 2 s.

## 8. Small conventions
Ids are 12 hex chars (`secrets.token_hex(6)`); history id = job id. Up to 4 jobs run at once (more →
"busy"); two restores into the same database are refused. Finished jobs stay in memory (newest 50) for
the Activity tab; history is the permanent record. After a successful backup the profile is saved and the
output folder becomes the default for new databases (`settings.output_dir`). A password typed for a server
without "Remember password" lives only in the job's memory.
