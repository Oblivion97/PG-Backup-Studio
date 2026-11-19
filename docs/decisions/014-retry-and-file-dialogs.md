# 014 — Retry a halted backup or restore; file dialogs in the browser

_Date: 2026-11-19 · Status: **approved by the user 2026-11-19** after trying it; released in v0.13.0 ("if a backup or restore halts due to the
network, give the user a Retry; in the browser the backup location Browse does not open Explorer; restore from PC
should also open Explorer and put the chosen file in the input field")._

1. **A dropped network no longer hangs a job.** Every connection sets TCP keepalives (`keepalives_idle` 30 s,
   `keepalives_interval` 10 s, `keepalives_count` 6). A connection that goes silent (Wi-Fi / VPN drop, server
   unplugged) is noticed within about 90 s; the job fails with the driver's message instead of waiting forever.
   A long statement on a busy server is not affected (the server's TCP stack still answers keepalives).
2. **Retry** (`POST /api/history/<id>/retry` → 202 `{job_id}`) starts a **failed, cancelled or interrupted** backup
   or restore again with the same settings. It works from the History entry, so it also works after the app was
   closed or crashed (`interrupted`). Every new backup / restore History entry keeps a `retry` field:
   - backup: the options without the file name (a retry writes a new timestamped file; the unfinished `.part` file
     of the failed attempt was already removed);
   - restore: the target (mode, owner, allow non-empty), never the typed confirmation. The file and the restore
     options are already in the entry.
   The new entry records `retry.from`; a second retry of the same entry while one runs is refused. Entries made by
   older versions have no `retry` and show no button. Transfers are not retried (out of scope; start them again from
   the Transfer tab). The server's password is asked again when it is not saved.
3. **Restore retry asks where to restore**, because the failed attempt may have left the database half restored:
   *Drop and recreate* (recommended; the database name must be typed, exactly as on the Restore page) or *Restore
   into it as it is* (non-empty allowed; "already exists" errors are expected). The original owner is kept.
4. **UI.** Activity cards and History rows of retryable jobs get a **Retry** button (highlighted when the error looks
   like a lost connection: "connection", "timeout", "server closed", "SSL SYSCALL", … or `interrupted`). A failed
   card with such an error explains: "The connection to the server was lost. Check the network or VPN, then press
   Retry."
5. **File dialogs in the browser.** The page cannot see the PC's folders, but the local server runs on that same PC
   and shows the operating system's dialog for it (`native_dialogs.py`):
   - Windows: the Explorer Open dialog (IFileOpenDialog via ctypes, its own STA thread, owned by a hidden top-most
     window so it opens above the browser). No new dependency, works when run from source too;
   - macOS: `osascript` choose folder / choose file;
   - Linux: `zenity` or `kdialog` if installed, otherwise no Browse button (`/api/app` → `dialogs: false`).
   The desktop window keeps pywebview's dialogs. One dialog at a time; a second request says one is already open.
6. **Restore "File on this PC" gets Browse…** (`POST /api/pick-file`): Explorer opens in the folder of the typed file,
   else the default backup folder, filtered to `.sql` / `.sql.gz` / `.gz` (All files available). The chosen path is
   put in the input field. Settings' Browse… and Import… now work in the browser too.
7. **A new default backup folder in Settings is used** (user report while trying it: "I changed the folder but the
   backup didn't go to the specified location"). Each database's backup profile remembered the folder of its last
   successful backup, and that overrode the default. Saving a *different* default folder in Settings now drops the
   remembered folder from every profile (`Store.forget_profile_folders`; the other remembered options stay), and an
   open Backup page switches to the new folder. A backup still saves the folder it used, so a folder typed on the
   Backup page for one database is remembered until the next change in Settings.
