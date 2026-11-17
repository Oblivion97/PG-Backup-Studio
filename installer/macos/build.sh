#!/usr/bin/env bash
# Build the macOS app and its disk image (decision 012). Run on a Mac with Python 3.14 and
#   pip install -r requirements-desktop.txt
# Result: dist/PG-Backup-Studio-<version>-macos-<arm64|x86_64>.dmg (CI: .github/workflows/build-mac-linux.yml)
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=${PYTHON:-python3}
VERSION=$($PY -c "import app; print(app.APP_VERSION)")
ARCH=$(uname -m)
echo "=== PG Backup Studio $VERSION for macOS $ARCH"

echo "=== Icon"
$PY installer/make_icon.py --png
rm -rf build/app.iconset && mkdir -p build/app.iconset
for s in 16 32 128 256 512; do
  cp "installer/icons/app-$s.png" "build/app.iconset/icon_${s}x${s}.png"
  cp "installer/icons/app-$((s * 2)).png" "build/app.iconset/icon_${s}x${s}@2x.png"
done
iconutil -c icns build/app.iconset -o installer/app.icns

echo "=== PyInstaller"
$PY -m PyInstaller --noconfirm --clean pg_backup_studio.spec
APP="dist/PG Backup Studio.app"
# ad-hoc signature (no Apple Developer ID yet): Apple silicon refuses unsigned code; users still confirm the
# first launch (right-click → Open, or System Settings → Privacy & Security → Open Anyway)
codesign --force --deep --sign - "$APP"

echo "=== Self test"
"$APP/Contents/MacOS/PG Backup Studio" --selftest

echo "=== Disk image"
STAGE=build/dmg
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
OUT="dist/PG-Backup-Studio-$VERSION-macos-$ARCH.dmg"
rm -f "$OUT"
hdiutil create -volname "PG Backup Studio $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$OUT"
echo "Done: $OUT"
