#!/usr/bin/env bash
# Usage: bash tests/test_image.sh [source.qcow2]
# Never passes the source to a writer. Requires ~12GB free for temporary copies.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $# -eq 1 ]] || { echo "Usage: bash tests/test_image.sh PATH_TO_QCOW2_IMAGE" >&2; exit 1; }
IMAGE="$(realpath -- "$1")"
WORK="$(mktemp -d /tmp/xbox-image-test-XXXXXX)"
trap 'rm -rf -- "$WORK"' EXIT
sha256sum "$IMAGE" > "$WORK/original.sha256"
qemu-img convert -f qcow2 -O raw "$IMAGE" "$WORK/copy.raw"
cmake -S "$HERE/driver/libfatx" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Debug > "$WORK/build.log"
cmake --build "$WORK/build" -j 4 >> "$WORK/build.log"
gcc -g -Wall -I "$HERE/driver/libfatx" "$HERE/tests/driver_io.c" "$WORK/build/libfatx.a" -o "$WORK/io"
gcc -g -Wall -I "$HERE/driver/libfatx" "$HERE/tests/persistence.c" "$WORK/build/libfatx.a" -o "$WORK/persistence"
"$WORK/io" "$WORK/copy.raw"
"$WORK/persistence" "$WORK/copy.raw" write
"$WORK/persistence" "$WORK/copy.raw" verify
qemu-img convert -f raw -O qcow2 "$WORK/copy.raw" "$WORK/modified.qcow2"
qemu-img check -f qcow2 "$WORK/modified.qcow2"
# Free the first RAW copy before the round trip, minimizing peak disk usage.
rm "$WORK/copy.raw"
qemu-img convert -f qcow2 -O raw "$WORK/modified.qcow2" "$WORK/roundtrip.raw"
"$WORK/persistence" "$WORK/roundtrip.raw" verify
sha256sum -c "$WORK/original.sha256"
printf 'Original unchanged. RAW/QCOW2/RAW round trip and permanent deletions verified.\n'
