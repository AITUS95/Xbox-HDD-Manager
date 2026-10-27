#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$HERE"
exec /usr/bin/python3 "$HERE/xbox_hdd_manager.py" "$@"
