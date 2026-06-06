#!/usr/bin/env bash
# Build BOTH the patched shim and the matching runsc from a gVisor checkout that
# has PR #13326 applied, for the host's bazel target arch. Ships them as a pair
# (the shim shells out to `runsc checkpoint`/`runsc restore`, so versions must
# match). Extracts from the bazel cache because `make copy` trips on a long
# xargs line.
#
# Usage: scripts/build-cr-binaries.sh /path/to/gvisor [/output/dir]
set -euo pipefail

GVISOR_SRC="${1:?usage: build-cr-binaries.sh /path/to/gvisor [outdir]}"
OUT="${2:-$(pwd)/out}"
mkdir -p "$OUT"
cd "$GVISOR_SRC"

echo "== building //shim:containerd-shim-runsc-v1 and //runsc:runsc =="
make copy TARGETS=//shim:containerd-shim-runsc-v1 DESTINATION="$OUT" || true
make copy TARGETS=//runsc:runsc                   DESTINATION="$OUT" || true

# Extract from the bazel cache volume if `make copy` did not place them.
VOL="$(docker volume ls --format '{{.Name}}' | grep '^gvisor-bazel-cache' | head -1)"
extract() { # <bin-name>
  local name="$1"
  [[ -f "$OUT/$name" ]] && return 0
  local p
  p="$(docker run --rm -v "$VOL":/cache alpine \
        sh -c "find /cache -type f -name '$name' -path '*bin/*' | head -1")"
  [[ -n "$p" ]] || { echo "ERROR: $name not found in bazel cache"; exit 1; }
  docker run --rm -v "$VOL":/cache -v "$OUT":/out alpine \
    sh -c "cp '$p' /out/$name && chmod +x /out/$name"
}
extract runsc
extract containerd-shim-runsc-v1

echo "== artifacts =="
( cd "$OUT" && sha256sum runsc containerd-shim-runsc-v1 && file runsc containerd-shim-runsc-v1 )
echo "pinned gVisor commit: $(git -C "$GVISOR_SRC" rev-parse HEAD)"
