#!/bin/sh
# Install and start the Linux packages on one distribution (CI runs this in a container per distro, decision 012).
#   installer/linux/smoke.sh <folder with the packages>
# Checks the .tar.gz and the AppImage everywhere, the .deb on apt systems and the .rpm on dnf / zypper systems,
# each with `--selftest` (server, page, API, PostgreSQL driver; no browser needed).
set -eu
PKG=$(cd "$1" && pwd)
ARCH=$(uname -m)
. /etc/os-release
echo "=== $PRETTY_NAME ($ARCH)"

if command -v apt-get >/dev/null; then PM=apt; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq tar gzip >/dev/null
elif command -v dnf >/dev/null; then PM=dnf; dnf install -y -q tar gzip findutils >/dev/null
elif command -v zypper >/dev/null; then PM=zypper; zypper --non-interactive -q install tar gzip >/dev/null
elif command -v pacman >/dev/null; then PM=pacman; pacman -Sy --noconfirm --needed tar gzip >/dev/null
else echo "no known package manager"; exit 1; fi
echo "glibc: $(ldd --version 2>&1 | head -1)"

echo "--- .tar.gz"
T=$(mktemp -d)
tar -xzf "$PKG"/pg-backup-studio-*-linux-"$ARCH".tar.gz -C "$T"
"$T/pg-backup-studio/pg-backup-studio" --selftest

echo "--- AppImage"
APPIMAGE=$(ls "$PKG"/PG-Backup-Studio-*-"$ARCH".AppImage)
chmod +x "$APPIMAGE"
APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGE" --selftest

case "$PM" in
  apt)
    echo "--- .deb"
    DEB_ARCH=$([ "$ARCH" = x86_64 ] && echo amd64 || echo arm64)
    # only our package: it has no required dependencies, and old releases (e.g. Debian 11, end of life) may no
    # longer serve the recommended xdg-utils stack
    apt-get install -y -qq --no-install-recommends "$PKG"/pg-backup-studio_*_"$DEB_ARCH".deb >/dev/null
    pg-backup-studio --selftest
    test -f /usr/share/applications/pg-backup-studio.desktop ;;
  dnf)
    echo "--- .rpm"
    dnf install -y -q "$PKG"/pg-backup-studio-*."$ARCH".rpm >/dev/null
    pg-backup-studio --selftest
    test -f /usr/share/applications/pg-backup-studio.desktop ;;
  zypper)
    echo "--- .rpm"
    zypper --non-interactive -q install --allow-unsigned-rpm "$PKG"/pg-backup-studio-*."$ARCH".rpm >/dev/null
    pg-backup-studio --selftest
    test -f /usr/share/applications/pg-backup-studio.desktop ;;
  pacman) echo "--- (no native package for Arch: .tar.gz and AppImage checked)" ;;
esac
echo "=== all good on $PRETTY_NAME"
