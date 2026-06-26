#!/usr/bin/env bash
# Build BOTH the patched shim and the matching runsc from a gVisor checkout that
# has PR #13326 applied. Ships them as a pair (the shim shells out to
# `runsc checkpoint`/`runsc restore`, so versions must match). Extracts from the
# bazel cache because `make copy` trips on a long xargs line.
#
# Target architecture is controlled by ARCH (gVisor's own convention):
#   ARCH=x86_64   -> linux/amd64
#   ARCH=aarch64  -> linux/arm64
# Default ARCH is the host arch. When ARCH differs from the host, gVisor builds
# through qemu (cross-build): it registers binfmt, builds its builder image with
# `--platform`, and runs bazel emulated — so an arm64 workstation can produce
# amd64 binaries (slow). See gVisor tools/images.mk for the cross-build notes.
#
# Usage: ARCH=x86_64 scripts/build-cr-binaries.sh /path/to/gvisor [/output/dir]
set -euo pipefail

GVISOR_SRC="${1:?usage: build-cr-binaries.sh /path/to/gvisor [outdir]}"
OUT="${2:-$(pwd)/out}"

# Normalize the host arch to gVisor's ARCH spelling (x86_64 / aarch64).
host_arch="$(uname -m)"
case "$host_arch" in
  arm64|aarch64) host_arch=aarch64 ;;
  amd64|x86_64)  host_arch=x86_64 ;;
esac
ARCH="${ARCH:-$host_arch}"
case "$ARCH" in
  amd64)  ARCH=x86_64 ;;
  arm64)  ARCH=aarch64 ;;
esac

mkdir -p "$OUT"
cd "$GVISOR_SRC"

echo "== building //shim:containerd-shim-runsc-v1 and //runsc:runsc for ARCH=$ARCH (host=$host_arch) =="
make ARCH="$ARCH" copy TARGETS=//shim:containerd-shim-runsc-v1 DESTINATION="$OUT" || true
make ARCH="$ARCH" copy TARGETS=//runsc:runsc                   DESTINATION="$OUT" || true

# If `make copy` did not place the binaries (it trips on a long xargs line), pull
# them straight out of the bazel cache. macOS keeps the cache in a Docker volume
# (gvisor-bazel-cache-<hash>-<ARCH>); Linux keeps it on a host path
# ($HOME/.cache/bazel). Handle both. The relpath under the cache is
# .../bin/<target-dir>/<name>, so match on it to avoid grabbing a test binary.
VOL="$(docker volume ls --format '{{.Name}}' | grep "^gvisor-bazel-cache.*-$ARCH$" | head -1)"
[[ -n "$VOL" ]] || VOL="$(docker volume ls --format '{{.Name}}' | grep '^gvisor-bazel-cache' | head -1)"
HOST_CACHE="${BAZEL_CACHE:-$HOME/.cache/bazel}"
extract() { # <bin-name> <target-subdir>
  local name="$1" sub="$2"
  [[ -f "$OUT/$name" ]] && return 0
  # macOS: search the cache volume from a throwaway container.
  if [[ -n "$VOL" ]]; then
    local p
    p="$(docker run --rm -v "$VOL":/cache alpine \
          sh -c "find /cache -type f -path '*/bin/$sub/$name' | head -1")"
    if [[ -n "$p" ]]; then
      docker run --rm -v "$VOL":/cache -v "$OUT":/out alpine \
        sh -c "cp '$p' /out/$name && chmod +x /out/$name"
      return 0
    fi
  fi
  # Linux: search the on-disk bazel cache directly.
  local hp
  hp="$(find "$HOST_CACHE" -type f -path "*/bin/$sub/$name" 2>/dev/null | head -1)"
  [[ -n "$hp" ]] || { echo "ERROR: $name not found in bazel cache (vol=$VOL dir=$HOST_CACHE)"; exit 1; }
  cp "$hp" "$OUT/$name" && chmod +x "$OUT/$name"
}
extract runsc runsc
extract containerd-shim-runsc-v1 shim

echo "== artifacts (ARCH=$ARCH) =="
( cd "$OUT" && sha256sum runsc containerd-shim-runsc-v1 && file runsc containerd-shim-runsc-v1 )
echo "pinned gVisor commit: $(git -C "$GVISOR_SRC" rev-parse HEAD)"
