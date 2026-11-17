# 012 — macOS and Linux builds

_Date: 2026-11-17 · Status: **requested by the user 2026-11-17** ("also build for apple linux release; linux should
also cover all major distros"). Released in v0.11.0._

1. **Same app, three platforms.** `desktop.py` and `pg_backup_studio.spec` know the platform; there is no second
   code base. PyInstaller cannot cross-compile, so macOS and Linux are built on GitHub Actions
   (`.github/workflows/build-mac-linux.yml`); Windows stays on `build_desktop.bat` + Inno Setup.
2. **macOS: native window** (pywebview on the system WKWebView), `.app` in a `.dmg`, one per architecture (arm64 on
   `macos-15`, x86_64 on `macos-15-intel`; no universal2 because psycopg-binary ships per-architecture wheels).
   Ad-hoc signed only: no Apple Developer ID yet, so the first launch needs right-click → Open (README says how).
   Data in `~/Library/Application Support/PG Backup Studio`.
3. **Linux: browser mode.** A native window would need GTK/WebKitGTK or Qt WebEngine, whose versions differ per
   distribution (or would add ~200 MB). The app opens in the default browser with the per-launch secret cookie, like
   the Windows fallback; the page shows **Quit** (`POST /api/app/quit`, asks first while work is running); Ctrl+C /
   SIGTERM also stop it; without a browser it prints the address. pywebview is neither installed nor bundled.
4. **"All major distros":** built in a `rockylinux:8` container (glibc 2.28) with Python built from source with a
   shared libpython, so the binary runs on glibc ≥ 2.28: RHEL/Rocky/Alma 8+, Debian 10+, Ubuntu 20.04+, Fedora,
   openSUSE Leap 15 / Tumbleweed, Arch. x86_64 and aarch64. Formats: **AppImage** (static runtime, no libfuse2),
   **.deb**, **.rpm** (nfpm; /opt/pg-backup-studio, /usr/bin link, desktop entry, hicolor icons) and **.tar.gz**.
   CI installs and starts each package in containers of 12 x86_64 and 4 aarch64 distribution images.
5. **One instance on every platform:** Windows named mutex + event (unchanged); macOS / Linux a locked file
   (`app.lock`) and a Unix socket (`app.sock`) in the data folder: a second launch tells the running app to reopen its
   tab (browser) or bring its window forward, then exits.
6. **`--selftest`**: starts the server on a temporary data folder and checks the page, every script, the API, the
   PostgreSQL driver (binary libpq), SCRAM hashing and the window library; exit code 0/1. Every build runs it, and the
   Linux install tests run it on each distribution. `--browser` forces browser mode anywhere.
7. **Icons:** `installer/make_icon.py --png` draws 16–1024 px PNGs (macOS `.icns` via iconutil, Linux hicolor).
