#!/usr/bin/env bash
# Install under a user-owned directory without changing system packages.
set -euo pipefail
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${XBOX_HDD_DIR:-$HOME/.local/share/xbox-hdd-manager}"
[[ $(id -u) != 0 ]] || { echo 'Run this installer as a normal user, not with sudo.' >&2; exit 1; }
for command in python3 cmake pkg-config cc qemu-img qemu-nbd pkexec fusermount fuser setfacl getfacl flock xdg-open; do
  command -v "$command" >/dev/null || { echo "Missing dependency: $command. See README.md." >&2; exit 1; }
done
pkg-config --exists fuse || { echo 'FUSE 2 development files are required. See README.md.' >&2; exit 1; }
/usr/bin/python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' || { echo 'GTK 3 Python bindings are required.' >&2; exit 1; }
if [[ -e "/run/xbox-hdd-manager/$(id -u).json" ]]; then
  echo 'Unmount and disconnect the active Xbox disk first.' >&2; exit 1
fi
if [[ -d "$TARGET" ]]; then
  echo "The destination already exists: $TARGET" >&2
  echo 'Choose a different XBOX_HDD_DIR. Existing backups and installations are never overwritten.' >&2
  exit 1
fi
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP="$DESKTOP_DIR/xbox-hdd-manager.desktop"
if [[ -e "$DESKTOP" ]]; then
  echo "An Xbox HDD Manager launcher already exists: $DESKTOP" >&2
  echo 'Remove or back up that launcher before installing another copy.' >&2; exit 1
fi
PARENT="$(dirname -- "$TARGET")"
mkdir -p "$PARENT"
STAGE="$(mktemp -d "$PARENT/.xbox-hdd-install-XXXXXX")"
trap 'rm -rf -- "$STAGE"' EXIT
mkdir -p "$STAGE/driver" "$STAGE/bin" "$STAGE/logs" "$STAGE/Mounts" "$STAGE/Backups"
cp -a "$SOURCE/driver/." "$STAGE/driver/"
cp -a "$SOURCE/tests" "$STAGE/tests"
find "$STAGE/tests" -type d -name __pycache__ -prune -exec rm -rf {} +
for file in hdd_core.py xbox_hdd_manager.py root_helper.py xbox-hdd-manager.sh patch_driver.py icon.png LICENSE README.md uninstall.sh update.sh update.py; do
  install -m 644 "$SOURCE/$file" "$STAGE/$file"
done
chmod 755 "$STAGE/xbox-hdd-manager.sh" "$STAGE/uninstall.sh" "$STAGE/update.sh"
cmake -S "$STAGE/driver/libfatx" -B "$STAGE/build-lib" -DCMAKE_INSTALL_PREFIX="$STAGE" -DCMAKE_BUILD_TYPE=Release
cmake --build "$STAGE/build-lib" -j "$(nproc)"
cmake --install "$STAGE/build-lib"
cmake -S "$STAGE/driver/fatxfs" -B "$STAGE/build-fuse" -DCMAKE_PREFIX_PATH="$STAGE" -DCMAKE_INSTALL_PREFIX="$STAGE" -DCMAKE_BUILD_TYPE=Release
cmake --build "$STAGE/build-fuse" -j "$(nproc)"
cmake --install "$STAGE/build-fuse"
[[ -x "$STAGE/bin/fatxfs" ]] || { echo 'FATX driver build failed.' >&2; exit 1; }
/usr/bin/python3 -m py_compile "$STAGE/hdd_core.py" "$STAGE/root_helper.py" "$STAGE/xbox_hdd_manager.py"
rm -rf "$STAGE/build-lib" "$STAGE/build-fuse" "$STAGE/include" "$STAGE/lib" "$STAGE/__pycache__"
# Desktop Exec values require escaping, including spaces and percent field codes.
/usr/bin/python3 - "$TARGET" "$STAGE" <<'PY'
from pathlib import Path
import sys
base, stage = map(Path, sys.argv[1:])
def quote(p):
    return str(p).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
(stage / 'xbox-hdd-manager.desktop').write_text(
    '[Desktop Entry]\nVersion=1.0\nType=Application\nName=Xbox HDD Manager\n'
    'Comment=Manage original Xbox FATX disk images\n'
    f'Exec="{quote(base / "xbox-hdd-manager.sh")}"\n'
    f'Icon={base / "icon.png"}\nTerminal=false\nCategories=Utility;\n')
PY
mv "$STAGE" "$TARGET"
mkdir -p "$DESKTOP_DIR"
install -m 644 "$TARGET/xbox-hdd-manager.desktop" "$DESKTOP"
if command -v update-desktop-database >/dev/null; then update-desktop-database "$DESKTOP_DIR"; fi
printf 'Installed to %s\nLaunch Xbox HDD Manager from your applications menu.\n' "$TARGET"
