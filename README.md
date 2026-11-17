# PG Backup Studio

A local app for Windows, macOS and Linux for backing up, restoring and managing PostgreSQL databases, with a
built-in SQL workspace. Backups are made from the system catalog by the app itself. No `pg_dump`, `psql` or other
PostgreSQL tools are needed.

## Download and install

Everything is on the [Releases](https://github.com/Oblivion97/PG-Backup-Studio/releases) page. Python and the
PostgreSQL driver come inside the app; nothing else is needed.

| System | Download | |
|---|---|---|
| Windows 10 / 11 (64-bit) | `PG Backup Studio Setup <version>.exe` | its own window |
| macOS 11+ Apple silicon (M1–M4) | `PG-Backup-Studio-<version>-macos-arm64.dmg` | its own window |
| macOS 11+ Intel | `PG-Backup-Studio-<version>-macos-x86_64.dmg` | its own window |
| Ubuntu, Debian, Linux Mint, Pop!_OS | `pg-backup-studio_<version>_amd64.deb` (`_arm64` for ARM) | opens in your browser |
| Fedora, RHEL, Rocky, AlmaLinux, openSUSE | `pg-backup-studio-<version>.x86_64.rpm` (`.aarch64` for ARM) | opens in your browser |
| Any Linux (Arch, Manjaro, …) | `PG-Backup-Studio-<version>-x86_64.AppImage` or `pg-backup-studio-<version>-linux-x86_64.tar.gz` | opens in your browser |

Linux builds run on any distribution with glibc 2.28 or newer (2018 onwards: RHEL 8, Debian 10, Ubuntu 20.04, …),
x86_64 or ARM64.

### Windows

1. Run the Setup. It installs for your Windows user without admin rights; choose "all users" in the first dialog if
   you want that instead.
2. Start **PG Backup Studio** from the Start menu (or the desktop icon if you ticked it).

**Requirements:** Windows 10 or 11 (64-bit) with Microsoft Edge WebView2. WebView2 is part of Windows 11. If it is
missing, the installer offers Microsoft's download page. Nothing else is needed: Python and the PostgreSQL driver
come inside the app.

**"Windows protected your PC":** the installer is not code-signed yet, so SmartScreen warns the first time.
Click **More info → Run anyway**.

**Updating:** run the newer Setup over the old one. Your servers, history and settings are kept.

**Uninstalling:** use Apps & features. It asks whether to delete your saved data (the default is to keep it).
Backup files are never deleted.

### macOS

1. Open the `.dmg` for your Mac (Apple menu → About This Mac: "Apple M…" = arm64, "Intel" = x86_64) and drag
   **PG Backup Studio** to **Applications**.
2. The app is not notarized by Apple yet, so the first start is blocked. Right-click (or Control-click) the app in
   Applications → **Open** → **Open**. On macOS 15 and later: try to open it once, then System Settings →
   Privacy & Security → **Open Anyway**. Or in Terminal: `xattr -dr com.apple.quarantine "/Applications/PG Backup Studio.app"`.

Data is kept in `~/Library/Application Support/PG Backup Studio`. Updating: replace the app in Applications.

### Linux

- **.deb:** `sudo apt install ./pg-backup-studio_<version>_amd64.deb`
- **.rpm:** `sudo dnf install ./pg-backup-studio-<version>.x86_64.rpm` (openSUSE:
  `sudo zypper install --allow-unsigned-rpm ./pg-backup-studio-<version>.x86_64.rpm`)
- **AppImage:** `chmod +x PG-Backup-Studio-*.AppImage` and run it (no install, no FUSE needed).
- **.tar.gz:** unpack anywhere and run `pg-backup-studio/pg-backup-studio`.

Start it from your applications menu or with `pg-backup-studio`. It opens in your default web browser (no GTK or Qt
needed). **Quit** is in the page's sidebar under Settings; closing the tab leaves it running, and starting it again
reopens the tab. Started from a terminal, Ctrl+C stops it too; on a machine without a desktop browser it prints the
address to open. Data is kept in `~/.config/PG Backup Studio`.

## Features

**Backup**
- Full, schema-only and data backups to one plain `.sql` file, written in a single consistent snapshot.
- Partial backups per table: full data, only the latest N rows, or structure only. A filterable list shows every
  table with its size.
- "Functions only" mode: one `.sql` file per function or procedure.
- Pre-check before a backup: permissions, table sizes, and whether "latest N" can use an index.
- Background jobs with live progress, a log, cancel, and a speed limit.

**Restore**
- Restore a backup into an existing or a new database, from history or from any `.sql` file on the PC.

**Database management**
- Add a server by pasting a connection link (`postgresql://user:password@host:5432/db?sslmode=require`, a JDBC
  URL, or `host=… port=… dbname=…`); host, port, user, password, database and SSL mode are filled in.
- Create, rename, clone and drop databases; see and end connections. System databases are protected.
- Remove a saved server with **Remove** next to **Edit server**, or right-click it in the sidebar → **Remove server**.
  Only the saved connection goes; nothing changes on the PostgreSQL server, and backup files and history are kept.
- Move servers to another PC: Settings → **Servers → Export…** saves the chosen servers to a `.json` file
  (passwords only if you tick **Include saved passwords**; they are then in the file as plain text). On the other
  PC, Settings → **Servers → Import…** adds them. Servers already saved there are skipped, and a name that is
  already taken gets " (2)" added.

**Server admin** (the **Admin** tab of a server; what it offers follows the saved login's rights)
- **Sessions:** everyone connected, with state, how long, client and query. Stale sessions (idle in transaction
  ≥ 5 min, idle ≥ 30 min; both adjustable) are flagged. Cancel a query or end sessions after a confirmation that lists
  them; background processes are never ended.
- **Roles & users:** create users and roles, set or remove passwords (sent only as a SCRAM-SHA-256 hash made on your
  PC, never saved), set attributes, connection limit and password expiry, grant and revoke roles (with admin option),
  rename and drop. Every change shows its SQL first and runs in one transaction.

**SQL workspace**
- Query tabs with syntax highlighting, suggestions (tables, columns, aliases, functions), a formatter and history.
- Auto-commit or manual transactions, with Commit, Rollback and Cancel.
- Results grid: sort, filter, resize, copy and export (CSV, JSON, SQL INSERT, Markdown).
- Edit table data in the grid, with a preview of the SQL before it runs.
- Explorer tree, table viewer, object details and DDL.
- EXPLAIN / EXPLAIN ANALYZE plan view. ANALYZE always runs in a transaction that is rolled back.

**Desktop app**
- Its own window. One copy runs at a time.
- Settings → **Default backup folder** has a **Browse…** button. The folder is checked to be writable when saved.
- It asks before closing while a backup is running or a query tab has an open transaction.
- If Microsoft Edge WebView2 cannot start (the window would stay blank), the app opens in your default browser
  instead, with a PG Backup Studio icon in the notification area (by the clock; Windows may tuck it under **^**).
  Click the icon to open the app in the browser again; right-click → **Quit** to stop it. Closing the browser tab
  does not stop the app, and starting it again from the Start menu reopens the tab. In this mode **Browse…** for
  folders is not available, so type the path.

## Where your data is kept

| | Installed app | Run from source |
|---|---|---|
| Servers, history, settings | `%APPDATA%\PG Backup Studio` | `data\` in this folder |
| Backups | the folder you choose in Settings | the folder you choose in Settings |

Settings → **App data** shows the folder, opens it, and has **Import…** to copy servers, history and settings over
from another data folder (for example the `data\` folder of a source checkout).

Server passwords are saved as plain text in `servers.json` in that folder. Keep it private. Passwords are never sent
back to the page.

The app only listens on `127.0.0.1`. The desktop app also uses a random port and a secret per launch, so other
programs and web pages cannot call it.

## Run from source

Needs Python (developed and tested with 3.14).

```
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:8000 in a browser (set `PORT` to use another port). To get the desktop window instead:

```
pip install -r requirements-desktop.txt
python desktop.py
```

## Build the installer

On the build PC only, install the build tools:

```
pip install -r requirements-desktop.txt
winget install JRSoftware.InnoSetup
```

Then run:

```
build_desktop.bat
```

It runs the quick tests, builds the app with PyInstaller, and writes
`dist\installer\PG Backup Studio Setup <version>.exe`. Add `--no-tests` to skip the tests. The version comes from
`APP_VERSION` in `app.py`.

## Tests

```
set PGPASSWORD=<postgres password>
python -m pytest -q
```

Tests that need PostgreSQL connect to `localhost:5432` as `postgres` and skip when `PGPASSWORD` is not set. They
only create databases and roles named `pgbs_test_*` and remove them afterwards.

## Project layout

| Path | What it is |
|---|---|
| `app.py` | Flask app and JSON API |
| `desktop.py` | Desktop launcher (window, single instance, shutdown) |
| `jobs.py` | Background backup and restore jobs |
| `store.py` | JSON storage for servers, history, settings and profiles |
| `engine/` | Catalog queries, backup, restore, pre-check, database management, SQL console, explorer |
| `static/` | The UI: `index.html` plus the workspace scripts |
| `installer/`, `pg_backup_studio.spec`, `build_desktop.bat` | Desktop build and installer |
| `tests/` | pytest suite |
| `docs/` | Architecture, design decisions, status and QA report |
