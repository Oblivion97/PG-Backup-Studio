# 010 — Step 9: desktop app and installer

_Date: 2026-10-29 · Status: **approved by the user 2026-10-29** ("Native window", "install it like regular software";
plus "an option in settings to set the backup location"). Design: ARCHITECTURE §15._

1. **Native window, same app.** `desktop.py` runs the existing Flask app in a background thread and shows it with
   pywebview (Edge WebView2 on Windows). No second UI, no Electron. `python app.py` still serves the browser version.
2. **Random port + per-launch secret.** The server binds 127.0.0.1 on a free port. The window opens
   `/launch/<token>` (32 random bytes), which sets an HttpOnly, SameSite=Strict cookie; with a token set, every
   `/api/` call needs that cookie on top of the Step 5 guard. Other local programs and web pages cannot use the API.
3. **Data in %APPDATA%\PG Backup Studio** for the installed (frozen) app, so updating or reinstalling never touches
   servers, history and settings. From source the app keeps `./data`. `PGBS_DATA_DIR` still overrides both. Moving
   over: Settings → App data → Import… copies the JSON files of another data folder (checked first, all or nothing).
4. **Backup folder setting.** Settings and the Backup form get a Browse… button (native folder picker, desktop only);
   the folder is checked to be writable when settings are saved.
5. **Closing** asks first when a backup / restore runs or a query tab has an open transaction, then cancels jobs,
   closes every database connection and quits. One instance at a time (named mutex, shared with the installer).
6. **Packaging:** PyInstaller one-folder build (fast start, fewer antivirus false alarms than one-file), no console,
   own icon and version info; numpy / shapely left out (optional psycopg adapters). Inno Setup installer: per-user
   install without admin by default (all users on request), Start-menu and optional desktop shortcut, uninstaller in
   Apps & features, updates in place (fixed AppId), refuses while the app runs, checks for WebView2, asks whether to
   delete user data on uninstall (default: keep). Backup files are never touched.
7. **Nothing to install on the user's PC** besides Windows 10/11 x64 with WebView2 (built into Windows 11): Python,
   Flask, psycopg and libpq are inside the build. The build tools (pywebview, PyInstaller, Inno Setup) are only needed
   on the PC that builds.
8. **Not signed.** SmartScreen shows "Unknown publisher" until a code-signing certificate is bought; signing can be
   added to `build_desktop.bat` later.
