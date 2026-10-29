"""Write installer/version_info.txt (the exe's Windows version resource) from app.APP_VERSION.

Run: python installer/make_version.py   (build_desktop.bat does this before PyInstaller)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import APP_VERSION  # noqa: E402

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'PG Backup Studio'),
      StringStruct('FileDescription', 'PG Backup Studio'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'PG Backup Studio'),
      StringStruct('OriginalFilename', 'PG Backup Studio.exe'),
      StringStruct('ProductName', 'PG Backup Studio'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> None:
    parts = [int(p) for p in APP_VERSION.split(".")][:4]
    parts += [0] * (4 - len(parts))
    target = ROOT / "installer" / "version_info.txt"
    target.write_text(TEMPLATE.format(t=tuple(parts), v=APP_VERSION), encoding="utf-8")
    print(f"wrote {target} for {APP_VERSION}")


if __name__ == "__main__":
    main()
