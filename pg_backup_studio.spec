# PyInstaller build of the desktop app (Step 9, decision 010): one-folder build, no console window.
# Build with build_desktop.bat (or: python -m PyInstaller --noconfirm --clean pg_backup_studio.spec).
# Output: dist/PG Backup Studio/PG Backup Studio.exe; the installer (installer/pg_backup_studio.iss) packs that folder.
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

APP_NAME = "PG Backup Studio"

hidden = collect_submodules("psycopg") + ["psycopg_binary", "psycopg_binary._psycopg", "psycopg_binary.pq"]
binaries = collect_dynamic_libs("psycopg_binary")
datas = [("static", "static")] + collect_data_files("psycopg_binary") + collect_data_files("webview")

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "_pytest", "tests", "PyInstaller", "numpy", "shapely"],   # numpy / shapely: optional psycopg adapters, unused
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon="installer/app.ico",
    version="installer/version_info.txt",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)
