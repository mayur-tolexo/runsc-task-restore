#!/usr/bin/env bash
# Build the patched containerd-shim-runsc-v1 from a gVisor checkout that has the
# patch applied. Uses gVisor's Bazel-in-Docker flow, then extracts the binary
# from the bazel cache (the stock `make copy` helper trips on a long xargs line).
#
# Usage: scripts/build.sh /path/to/gvisor [/output/dir]
set -euo pipefail

GVISOR_SRC="${1:?usage: build.sh /path/to/gvisor [outdir]}"
OUT="${2:-$(pwd)/out}"
mkdir -p "$OUT"

cd "$GVISOR_SRC"

# Run the bazel build (artifact lands in the bazel cache even if `copy` errors).
make copy TARGETS=//shim:containerd-shim-runsc-v1 DESTINATION="$OUT" || true

# Locate and extract the built binary from the bazel cache volume.
BIN="$OUT/containerd-shim-runsc-v1"
if [[ ! -f "$BIN" ]]; then
  VOL="$(docker volume ls --format '{{.Name}}' | grep '^gvisor-bazel-cache' | head -1)"
  [[ -n "$VOL" ]] || { echo "no gvisor-bazel-cache volume found"; exit 1; }
  CACHE_BIN="$(docker run --rm -v "$VOL":/cache alpine \
    sh -c 'find /cache -type f -name containerd-shim-runsc-v1 | head -1')"
  [[ -n "$CACHE_BIN" ]] || { echo "shim not found in bazel cache"; exit 1; }
  docker run --rm -v "$VOL":/cache -v "$OUT":/out alpine \
    sh -c "cp '$CACHE_BIN' /out/containerd-shim-runsc-v1 && chmod +x /out/containerd-shim-runsc-v1"
fi

file "$BIN"
echo "built: $BIN"
