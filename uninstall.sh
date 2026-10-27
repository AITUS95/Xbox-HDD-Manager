#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[[ -x "$HERE/bin/fatxfs" && -f "$HERE/hdd_core.py" ]] || { echo 'Run this script from the installed application directory.' >&2; exit 1; }
exec 9>"$HERE/manager.lock"
flock -n 9 || { echo 'Close the application first.' >&2; exit 1; }
if [[ -e "/run/xbox-hdd-manager/$(id -u).json" ]] || /usr/bin/python3 - "$HERE" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from hdd_core import layout_paths
root = str(layout_paths(sys.argv[1])[0])
for line in Path('/proc/self/mountinfo').read_text().splitlines():
    path = line.split()[4].replace('\\040', ' ')
    if path == root or path.startswith(root + '/'):
        sys.exit(0)
sys.exit(1)
PY
then
  echo 'Unmount and disconnect the disk first.' >&2; exit 1
fi
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/xbox-hdd-manager.desktop"
# Do not delete another installation's launcher.
if [[ -f "$DESKTOP" ]] && cmp -s "$DESKTOP" "$HERE/xbox-hdd-manager.desktop"; then rm "$DESKTOP"; fi
rm -f "$HERE/hdd_core.py" "$HERE/xbox_hdd_manager.py" "$HERE/root_helper.py" "$HERE/xbox-hdd-manager.sh" "$HERE/patch_driver.py" "$HERE/update.py" "$HERE/update.sh" "$HERE/icon.png" "$HERE/xbox-hdd-manager.desktop" "$HERE/uninstall.sh"
rm -rf "$HERE/bin" "$HERE/driver" "$HERE/__pycache__"
echo "Application removed. Backups, mounts, logs and documentation remain in $HERE."
