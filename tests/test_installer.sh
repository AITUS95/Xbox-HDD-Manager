#!/usr/bin/env bash
# Compile and install in a temporary directory without touching the real launcher.
set -euo pipefail
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d /tmp/xbox-install-test-XXXXXX)"
trap 'status=$?; if (( status )); then cat "$WORK"/*.log; fi; rm -rf -- "$WORK"' EXIT
export XBOX_HDD_DIR="$WORK/Example Directory/Xbox HDD Manager"
export XDG_DATA_HOME="$WORK/data"
bash "$SOURCE/install.sh" > "$WORK/install.log" 2>&1
[[ -x "$XBOX_HDD_DIR/bin/fatxfs" && -x "$XBOX_HDD_DIR/xbox-hdd-manager.sh" ]]
[[ -f "$XDG_DATA_HOME/applications/xbox-hdd-manager.desktop" ]]
desktop-file-validate "$XDG_DATA_HOME/applications/xbox-hdd-manager.desktop"
printf 'keep' > "$XBOX_HDD_DIR/Backups/backup.qcow2"
if bash "$SOURCE/install.sh" > "$WORK/duplicate.log" 2>&1; then
    echo 'Installer overwrote an existing installation.' >&2; exit 1
fi
bash "$XBOX_HDD_DIR/uninstall.sh" > "$WORK/uninstall.log" 2>&1
[[ ! -e "$XBOX_HDD_DIR/bin/fatxfs" && ! -e "$XDG_DATA_HOME/applications/xbox-hdd-manager.desktop" ]]
[[ "$(cat "$XBOX_HDD_DIR/Backups/backup.qcow2")" == 'keep' ]]
printf 'PASS: installation, real driver build, desktop file, duplicate refusal, backup-preserving uninstall\n'
