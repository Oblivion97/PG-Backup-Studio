"""Desktop app (Step 9, decisions 010 and 012): the Flask app in a native window, or in the default browser.

The server runs in a background thread on a free 127.0.0.1 port; the window (pywebview: Edge WebView2 on Windows,
WKWebView on macOS) opens `/launch/<token>`, which hands the page a per-launch secret cookie that every API call must
carry. Closing the window asks first when a backup / restore is running or a query tab has an open transaction,
then stops the jobs, closes every database connection and quits. One instance at a time; launching again brings
the running app forward (window) or reopens its tab (browser).

Browser mode: on Linux always (a native window would need GTK or Qt, which differ between distributions), with
`--browser` anywhere, and as the fallback when the window cannot start (e.g. WebView2 failing on Windows). The same
launch URL opens in the default browser. On Windows a tray icon (Open / Quit) keeps the app running; on macOS and
Linux the page shows a Quit button (and Ctrl+C / SIGTERM stop it when started from a terminal).

`--selftest` starts the server on a temporary data folder, checks the page, the scripts, the API and the PostgreSQL
driver, and exits 0 / 1 without opening anything (used by the builds on every platform).

Run from source with `python desktop.py`; the installed app is this file frozen by PyInstaller
(`pg_backup_studio.spec`), which keeps its data in the user's app-data folder (store.default_data_dir).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import secrets
import signal
import socket
import sys
import tempfile
import threading

from werkzeug.serving import make_server

import app as web
from store import APP_NAME, Store

try:   # not bundled on Linux (browser mode only)
    import webview
except Exception:   # noqa: BLE001
    webview = None

log = logging.getLogger("desktop")

MUTEX_NAME = "Local\\PGBackupStudio.SingleInstance"   # also in installer/pg_backup_studio.iss (AppMutex)
WINDOW_SIZE = (1440, 920)
MIN_SIZE = (980, 640)
TRAY = os.name == "nt"   # browser mode keeps running behind a tray icon (Windows) or until the page's Quit (others)
close_question = web.close_question


class Bridge:
    """What the Flask app may ask of the desktop side (passed to create_app as `desktop`)."""

    def __init__(self):
        self.window = None
        self.in_browser = False   # True in browser mode: there is no window to show a dialog on
        self.quit_event = threading.Event()

    @property
    def can_quit(self) -> bool:
        """The page offers Quit: browser mode without a tray icon (macOS / Linux)."""
        return self.in_browser and not TRAY

    def request_quit(self) -> None:
        # a moment later, so the HTTP answer still reaches the page
        threading.Timer(0.3, self.quit_event.set).start()

    def pick_folder(self, start: str) -> str | None:
        """Native folder picker; None when cancelled."""
        if self.in_browser:
            raise web.ApiError("PG Backup Studio is running in your browser, which cannot show a folder dialog; "
                               "type the path instead.", "invalid")
        if self.window is None or webview is None:
            return None
        chosen = self.window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        return chosen[0] if chosen else None


# ---------------------------------------------------------------- one instance at a time

def single_instance(data_dir: str | None = None) -> object | None:
    """Held for the life of the process (named mutex on Windows, a locked file elsewhere); None when another
    instance already holds it."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return handle
    import fcntl
    os.makedirs(data_dir, exist_ok=True)
    f = open(os.path.join(data_dir, "app.lock"), "a+")   # noqa: SIM115 (kept open on purpose)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def already_running_message() -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"{APP_NAME} is already open.", APP_NAME, 0x40)   # MB_ICONINFORMATION
    else:
        print(f"{APP_NAME} is already open.", file=sys.stderr)


OPEN_EVENT_NAME = "Local\\PGBackupStudio.OpenInBrowser"   # Windows: a second launch sets it


def _socket_path(data_dir: str) -> str:
    path = os.path.join(data_dir, "app.sock")
    if len(path.encode()) > 100:   # AF_UNIX path limit (104 on macOS, 108 on Linux)
        path = os.path.join(tempfile.gettempdir(), f"pgbs-{os.getuid()}.sock")
    return path


def signal_running_instance(data_dir: str | None = None) -> bool:
    """Second launch: ask the running app to come forward (reopen its tab / show its window). False when it does
    not listen (Windows: it runs in a window, which then says "already open")."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenEventW(0x0002, False, OPEN_EVENT_NAME)   # EVENT_MODIFY_STATE
        if not handle:
            return False
        kernel32.SetEvent(handle)
        kernel32.CloseHandle(handle)
        return True
    if not data_dir:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(_socket_path(data_dir))
            s.sendall(b"open")
        return True
    except OSError:
        return False


def listen_for_second_launch(on_open, data_dir: str | None = None) -> None:
    """Call `on_open` whenever another launch (Start menu, Dock, desktop icon) signals the running app."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateEventW(None, False, False, OPEN_EVENT_NAME)   # auto-reset; held for the life of the app

        def wait():
            while kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:   # INFINITE → WAIT_OBJECT_0
                on_open()
    else:
        if not data_dir:
            return
        path = _socket_path(data_dir)
        try:
            os.unlink(path)   # left by a crashed run; we hold the instance lock, so it is not in use
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(4)

        def wait():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(2)
                    try:
                        if conn.recv(16) == b"open":
                            on_open()
                    except OSError:
                        pass

    threading.Thread(target=wait, name="second-launch", daemon=True).start()


# ---------------------------------------------------------------- logging, WebView2 failure, message boxes

def setup_logging(data_dir) -> None:
    """The window has no console: log to <data>/logs/desktop.log (rotated), and to stderr when there is one."""
    folder = os.path.join(data_dir, "logs")
    os.makedirs(folder, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        os.path.join(folder, "desktop.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


WEBVIEW2_FAILED = "WebView2 initialization failed"   # pywebview logs this (edgechromium.py) and leaves the window blank


class WebViewFailure(logging.Handler):
    """Notices when the window's browser engine (Edge WebView2) cannot start.

    pywebview has no failure callback: it logs an error and keeps showing an empty window. This handler watches the
    `pywebview` logger for that error and calls `on_fail` once (on pywebview's UI thread).
    """

    def __init__(self, on_fail):
        super().__init__(level=logging.ERROR)
        self.on_fail, self.failed = on_fail, False

    def emit(self, record: logging.LogRecord) -> None:
        if self.failed or WEBVIEW2_FAILED not in record.getMessage():
            return
        self.failed = True
        self.on_fail()


def message_box(text: str, flags: int) -> int:
    """A native message box (Windows); returns the button pressed. Elsewhere: print and answer OK/Yes."""
    if os.name == "nt":
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags | 0x10000)   # MB_SETFOREGROUND
    print(text, file=sys.stderr)
    return 1


MB_YESNO_WARN, IDYES = 0x34, 6


def quit_allowed(manager, consoles) -> bool:
    """Tray → Quit: fine when nothing is running, otherwise ask (like closing the window)."""
    reason = close_question(manager, consoles)
    return not reason or message_box(f"Quit {APP_NAME}?\n\n{reason}", MB_YESNO_WARN) == IDYES


# ---------------------------------------------------------------- Windows tray icon (browser mode)

def tray_icon_image():
    """The app icon for the tray: the exe's own icon when installed, installer/app.ico from source."""
    from System.Drawing import Icon, Size, SystemIcons
    try:
        if getattr(sys, "frozen", False):
            return Icon.ExtractAssociatedIcon(sys.executable)
        ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "installer", "app.ico")
        if os.path.exists(ico):
            return Icon(ico, Size(16, 16))
    except Exception:
        log.exception("could not load the tray icon image")
    return SystemIcons.Application


def run_tray(open_browser, may_quit) -> None:
    """Notification-area icon (WinForms NotifyIcon through pythonnet, which pywebview already loads).

    Click: open the app in the browser. Right-click: Open / Quit. Blocks until Quit is chosen and allowed; closing
    the browser tab does not stop the app.
    """
    import clr
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    from System.Drawing import Font, FontStyle
    from System.Windows.Forms import (Application, ContextMenuStrip, MouseButtons, NotifyIcon, ToolStripMenuItem,
                                      ToolStripSeparator)

    icon = NotifyIcon()
    icon.Icon = tray_icon_image()
    icon.Text = f"{APP_NAME} (running in your browser)"   # Windows allows up to 63 characters

    def on_click(sender, args):
        if args.Button == MouseButtons.Left:
            open_browser()

    def on_quit(sender, args):
        if may_quit():
            icon.Visible = False
            Application.ExitThread()

    open_item = ToolStripMenuItem(f"Open {APP_NAME}")
    open_item.Font = Font(open_item.Font, FontStyle.Bold)
    open_item.Click += lambda sender, args: open_browser()
    quit_item = ToolStripMenuItem("Quit")
    quit_item.Click += on_quit
    menu = ContextMenuStrip()
    menu.Items.Add(open_item)
    menu.Items.Add(ToolStripSeparator())
    menu.Items.Add(quit_item)
    icon.ContextMenuStrip = menu
    icon.MouseClick += on_click
    icon.Visible = True
    try:
        Application.Run()
    finally:
        icon.Visible = False
        icon.Dispose()


# ---------------------------------------------------------------- browser mode

def _say(text: str) -> None:
    if sys.stdout is not None:
        print(text, flush=True)


def run_in_browser(url: str, manager, consoles, bridge: Bridge | None = None, data_dir: str | None = None) -> None:
    """Open the app in the default browser and keep the server up until Quit (tray icon, or the page's button).

    The launch URL sets the per-launch secret cookie in that browser, exactly as it would in the window; it is used
    for every reopen too, so the app still works after the browser has been closed and its session cookie is gone.
    """
    import webbrowser

    def open_browser():
        if not webbrowser.open(url):
            log.warning("no web browser could be started; open %s yourself", url.split("/launch/")[0])
            _say(f"Open this address in a web browser on this computer:\n  {url}")

    log.info("running in the default browser")
    open_browser()
    listen_for_second_launch(open_browser, data_dir)
    if TRAY:
        run_tray(open_browser, lambda: quit_allowed(manager, consoles))
        log.info("quit from the tray icon")
        return
    stop = bridge.quit_event if bridge is not None else threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):   # not the main thread (tests)
            pass
    _say(f"{APP_NAME} is running in your web browser. Use Quit in the page (or press Ctrl+C here) to stop it.")
    while not stop.wait(0.5):
        pass
    log.info("quit from the page")


# ---------------------------------------------------------------- self test (builds)

def selftest() -> int:
    """Start the server on a temporary data folder and check what a user would need; 0 when all is well."""
    import json
    import urllib.request

    results: list[tuple[str, bool, str]] = []

    def check(name, fn):
        try:
            results.append((name, True, str(fn())))
        except Exception as e:   # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"))

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="pgbs-selftest-") as d:
        store = Store(d)
        flask_app = web.create_app(store)
        server = make_server("127.0.0.1", 0, flask_app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"

        def get(path, headers=None):
            with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers or {}), timeout=10) as r:
                return r.status, r.read().decode("utf-8")

        def page():
            status, text = get("/")
            assert status == 200 and "PG Backup Studio" in text
            return f"{len(text)} bytes"

        def scripts():
            for name in web.WORKSPACE_SCRIPTS:
                assert get("/ws/" + name)[0] == 200, name
            return ", ".join(web.WORKSPACE_SCRIPTS)

        def api():
            info = json.loads(get("/api/app", {"X-PGBS": "1"})[1])
            assert info["version"] == web.APP_VERSION
            return "version " + info["version"]

        def driver():
            import psycopg
            assert psycopg.pq.__impl__ in ("binary", "c"), psycopg.pq.__impl__
            return f"psycopg {psycopg.__version__}, libpq {psycopg.pq.version()}, {psycopg.pq.__impl__}"

        def passwords():
            from engine.admin import scram_verifier
            assert scram_verifier("x").startswith("SCRAM-SHA-256$4096:")
            return "SCRAM-SHA-256"

        def window():
            if sys.platform.startswith("linux"):
                return "browser mode (Linux)"
            assert webview is not None, "pywebview is missing"
            return f"pywebview {getattr(webview, '__version__', '')}".strip()

        for name, fn in (("page", page), ("scripts", scripts), ("api", api), ("postgresql driver", driver),
                         ("passwords", passwords), ("window", window)):
            check(name, fn)
        server.shutdown()
        flask_app.config["PGBS_MANAGER"].shutdown()
        flask_app.config["PGBS_CONSOLES"].close_all()
    ok = all(r[1] for r in results)
    _say(f"{APP_NAME} {web.APP_VERSION} self test on {sys.platform}: {'OK' if ok else 'FAILED'}")
    for name, good, detail in results:
        _say(f"  {'ok  ' if good else 'FAIL'} {name}: {detail}")
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    browser_only = "--browser" in argv or webview is None or sys.platform.startswith("linux")
    store = Store()
    data_dir = str(store.dir)
    lock = single_instance(data_dir)
    if lock is None:
        if not signal_running_instance(data_dir):   # it comes forward when it can; otherwise say so
            already_running_message()
        return 0
    setup_logging(data_dir)
    bridge = Bridge()
    token = secrets.token_urlsafe(32)
    flask_app = web.create_app(store, desktop=bridge, launch_token=token)
    manager = flask_app.config["PGBS_MANAGER"]
    consoles = flask_app.config["PGBS_CONSOLES"]
    server = make_server("127.0.0.1", 0, flask_app, threaded=True)
    port = server.server_port
    threading.Thread(target=server.serve_forever, name="flask", daemon=True).start()
    log.info("%s %s on 127.0.0.1:%s, data in %s (%s)", APP_NAME, web.APP_VERSION, port, data_dir, sys.platform)
    url = f"http://127.0.0.1:{port}/launch/{token}"
    try:
        if browser_only:
            bridge.in_browser = True
            run_in_browser(url, manager, consoles, bridge, data_dir)
        else:
            run_window(url, bridge, manager, consoles, data_dir)
    finally:
        log.info("stopping")
        manager.shutdown()
        consoles.close_all()
        server.shutdown()
    return 0


def run_window(url: str, bridge: Bridge, manager, consoles, data_dir: str) -> None:
    """The native window; falls back to browser mode when it cannot start."""
    # downloads (server export, grid CSV/JSON/... exports) get a Save dialog; pywebview cancels them by default
    webview.settings["ALLOW_DOWNLOADS"] = True
    window = webview.create_window(APP_NAME, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=MIN_SIZE,
                                   text_select=True, zoomable=True, background_color="#ffffff")
    bridge.window = window

    def on_closing():
        reason = close_question(manager, consoles)
        if reason and not window.create_confirmation_dialog(f"Close {APP_NAME}?", reason):
            return False   # stay open
        return True

    window.events.closing += on_closing

    def close_blank_window():
        # called on the UI thread from inside pywebview's callback: close from another thread, skipping the question
        window.events.closing -= on_closing
        threading.Thread(target=window.destroy, name="close-blank-window", daemon=True).start()

    def bring_forward():
        for step in ("restore", "show"):
            try:
                getattr(window, step)()
            except Exception:   # noqa: BLE001
                pass

    if os.name != "nt":   # Windows: a second launch says "already open" while the window is up
        listen_for_second_launch(bring_forward, data_dir)
    watch = WebViewFailure(close_blank_window)
    logging.getLogger("pywebview").addHandler(watch)
    try:
        # private_mode off + a storage folder: the page's own settings (theme, query tabs, history) survive restarts
        webview.start(private_mode=False, storage_path=os.path.join(data_dir, "webview"))
    except Exception:
        log.exception("the window could not be created")
        watch.failed = True
    if watch.failed:
        bridge.window, bridge.in_browser = None, True
        log.warning("window could not start; running in the default browser instead")
        run_in_browser(url, manager, consoles, bridge, data_dir if os.name != "nt" else None)
    else:
        log.info("window closed")


if __name__ == "__main__":
    sys.exit(main())
