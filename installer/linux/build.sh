#!/usr/bin/env bash
# Build the Linux app and its packages (decision 012): AppImage, .deb, .rpm and .tar.gz.
# Run on the OLDEST distribution to support, because the result needs that glibc or newer. CI builds in a
# rockylinux:8 container (glibc 2.28: RHEL/Rocky/Alma 8+, Debian 10+, Ubuntu 20.04+, Fedora, openSUSE Leap 15, Arch)
# with Python 3.14 built from source with --enable-shared (PyInstaller needs libpython).
#   PYTHON=/opt/python/bin/python3 installer/linux/build.sh
# Needs curl (downloads appimagetool and nfpm). Result: dist/packages/
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=${PYTHON:-python3}
VERSION=$($PY -c "import app; print(app.APP_VERSION)")
ARCH=$(uname -m)                                   # x86_64 | aarch64
case "$ARCH" in x86_64) DEB_ARCH=amd64 ;; aarch64) DEB_ARCH=arm64 ;; *) echo "unsupported: $ARCH"; exit 1 ;; esac
NAME=pg-backup-studio
OUT=dist/packages
echo "=== PG Backup Studio $VERSION for Linux $ARCH (glibc $(ldd --version | head -1 | awk '{print $NF}'))"

echo "=== Icons"
$PY installer/make_icon.py --png

echo "=== PyInstaller"
$PY -m PyInstaller --noconfirm --clean pg_backup_studio.spec
echo "=== Self test"
"dist/$NAME/$NAME" --selftest

rm -rf "$OUT" && mkdir -p "$OUT" build/tools
DESKTOP=build/$NAME.desktop
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=PG Backup Studio
GenericName=PostgreSQL backup and management
Comment=Back up, restore and manage PostgreSQL databases (opens in your web browser)
Exec=$NAME
Icon=$NAME
Terminal=false
Categories=Development;Database;
Keywords=PostgreSQL;Postgres;backup;restore;database;SQL;
StartupNotify=false
EOF

echo "=== .tar.gz"
tar -C dist -czf "$OUT/$NAME-$VERSION-linux-$ARCH.tar.gz" "$NAME"

echo "=== AppImage"
APPDIR=build/AppDir
rm -rf "$APPDIR" && mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications"
cp -a "dist/$NAME" "$APPDIR/usr/lib/$NAME"
cp "$DESKTOP" "$APPDIR/$NAME.desktop"
cp "$DESKTOP" "$APPDIR/usr/share/applications/"
cp installer/icons/app-256.png "$APPDIR/$NAME.png"
for s in 16 32 48 64 128 256 512; do
  mkdir -p "$APPDIR/usr/share/icons/hicolor/${s}x${s}/apps"
  cp "installer/icons/app-$s.png" "$APPDIR/usr/share/icons/hicolor/${s}x${s}/apps/$NAME.png"
done
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/pg-backup-studio/pg-backup-studio" "$@"
EOF
chmod +x "$APPDIR/AppRun"
TOOL=build/tools/appimagetool-$ARCH.AppImage
[ -x "$TOOL" ] || { curl -fsSL -o "$TOOL" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"; chmod +x "$TOOL"; }
# no FUSE inside containers: run the tool extracted; the AppImage it makes uses the static runtime (no libfuse2 needed)
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=$ARCH "$TOOL" --no-appstream "$APPDIR" "$OUT/PG-Backup-Studio-$VERSION-$ARCH.AppImage"

echo "=== .deb and .rpm"
NFPM=build/tools/nfpm
if [ ! -x "$NFPM" ]; then
  NFPM_VER=${NFPM_VERSION:-$(curl -fsSL ${GITHUB_TOKEN:+-H "Authorization: Bearer $GITHUB_TOKEN"} https://api.github.com/repos/goreleaser/nfpm/releases/latest | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p')}
  case "$ARCH" in x86_64) NARCH=x86_64 ;; aarch64) NARCH=arm64 ;; esac
  curl -fsSL "https://github.com/goreleaser/nfpm/releases/download/v$NFPM_VER/nfpm_${NFPM_VER}_Linux_$NARCH.tar.gz" | tar -xz -C build/tools nfpm
fi
ICONS=""
for s in 16 32 48 64 128 256 512; do
  ICONS="$ICONS
  - src: installer/icons/app-$s.png
    dst: /usr/share/icons/hicolor/${s}x${s}/apps/$NAME.png"
done
cat > build/nfpm.yaml <<EOF
name: $NAME
arch: $DEB_ARCH
platform: linux
version: $VERSION
section: database
priority: optional
maintainer: "PG Backup Studio <Oblivion97@users.noreply.github.com>"
description: |
  Back up, restore and manage PostgreSQL databases, with a SQL workspace and server admin.
  Backups are made from the system catalog; no pg_dump or psql is needed. Opens in your web browser.
vendor: PG Backup Studio
homepage: https://github.com/Oblivion97/PG-Backup-Studio
recommends:
  - xdg-utils
contents:
  - src: dist/$NAME/
    dst: /opt/$NAME/
    type: tree
  - src: /opt/$NAME/$NAME
    dst: /usr/bin/$NAME
    type: symlink
  - src: $DESKTOP
    dst: /usr/share/applications/$NAME.desktop$ICONS
EOF
"$NFPM" package --config build/nfpm.yaml --packager deb --target "$OUT/${NAME}_${VERSION}_${DEB_ARCH}.deb"
"$NFPM" package --config build/nfpm.yaml --packager rpm --target "$OUT/$NAME-$VERSION.$ARCH.rpm"

ls -la "$OUT"
echo "Done: $OUT"
