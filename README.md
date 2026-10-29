# PG Backup Studio

A local Windows app for backing up, restoring and managing PostgreSQL databases, with a built-in SQL workspace.
Backups are made from the system catalog by the app itself. No `pg_dump`, `psql` or other PostgreSQL tools
are needed.

## Download and install

1. Download **`PG Backup Studio Setup <version>.exe`** from the
   [Releases](https://github.com/Oblivion97/PG-Backup-Studio/releases) page.
2. Run it. It installs for your Windows user without admin rights; choose "all users" in the first dialog if you
   want that instead.
3. Start **PG Backup Studio** from the Start menu (or the desktop icon if you ticked it).

**Requirements:** Windows 10 or 11 (64-bit) with Microsoft Edge WebView2. WebView2 is part of Windows 11. If it is
missing, the installer offers Microsoft's download page. Nothing else is needed: Python and the PostgreSQL driver
come inside the app.

**"Windows protected your PC":** the installer is not code-signed yet, so SmartScreen warns the first time.
Click **More info → Run anyway**.

**Updating:** run the newer Setup over the old one. Your servers, history and settings are kept.

**Uninstalling:** use Apps & features. It asks whether to delete your saved data (the default is to keep it).
Backup files are never deleted.

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
