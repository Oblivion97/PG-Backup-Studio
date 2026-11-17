# PyInstaller build of the desktop app (decisions 010 and 012): one-folder build, no console window.
# Windows: build_desktop.bat (then Inno Setup) → dist/PG Backup Studio/PG Backup Studio.exe
# macOS:   installer/macos/build.sh → dist/PG Backup Studio.app (then a .dmg)
# Linux:   installer/linux/build.sh → dist/pg-backup-studio/pg-backup-studio (then AppImage, .deb, .rpm, .tar.gz)
# Or directly: python -m PyInstaller --noconfirm --clean pg_backup_studio.spec
import re
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

APP_NAME = "PG Backup Studio"
MAC, LINUX = sys.platform == "darwin", sys.platform.startswith("linux")
VERSION = re.search(r'^APP_VERSION = "(.+?)"', open("app.py", encoding="utf-8").read(), re.M).group(1)

hidden = collect_submodules("psycopg") + ["psycopg_binary", "psycopg_binary._psycopg", "psycopg_binary.pq"]
binaries = collect_dynamic_libs("psycopg_binary")
datas = [("static", "static")] + collect_data_files("psycopg_binary")
excludes = ["tkinter", "pytest", "_pytest", "tests", "PyInstaller", "numpy", "shapely"]   # numpy / shapely: optional psycopg adapters, unused
if LINUX:
    excludes += ["webview", "clr", "clr_loader", "pythonnet"]   # browser mode only on Linux (decision 012)
else:
    datas += collect_data_files("webview")

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pg-backup-studio" if LINUX else APP_NAME,
    console=False,
    icon="installer/app.icns" if MAC else None if LINUX else "installer/app.ico",
    version=None if MAC or LINUX else "installer/version_info.txt",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="pg-backup-studio" if LINUX else APP_NAME, upx=False)
if MAC:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon="installer/app.icns",
        bundle_identifier="io.github.oblivion97.pgbackupstudio",
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSHumanReadableCopyright": "PG Backup Studio",
            "LSApplicationCategoryType": "public.app-category.developer-tools",
        },
    )
