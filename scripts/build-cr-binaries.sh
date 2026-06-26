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
# ($HOME/.cache/bazel). Handle both. Match the binary by name anywhere under a
# bin/ dir and pick the largest hit — rules_go nests the real binary under a
# <name>_/ dir, and -type f skips the convenience symlink.
VOL="$(docker volume ls --format '{{.Name}}' | grep "^gvisor-bazel-cache.*-$ARCH$" | head -1 || true)"
[[ -n "$VOL" ]] || VOL="$(docker volume ls --format '{{.Name}}' | grep '^gvisor-bazel-cache' | head -1 || true)"
HOST_CACHE="${BAZEL_CACHE:-$HOME/.cache/bazel}"

# pick_largest <root> <name>: print the largest regular file named <name> under a
# bin/ dir in <root>. Uses a wc -c loop (no `head`) so a SIGPIPE under pipefail
# can never abort the script mid-extract.
pick_largest() {
  local root="$1" name="$2" best="" bestsz=-1 f sz
  while IFS= read -r f; do
    sz="$(wc -c <"$f" 2>/dev/null || echo 0)"
    if [ "$sz" -gt "$bestsz" ]; then bestsz="$sz"; best="$f"; fi
  done < <(find "$root" -type f -name "$name" -path '*/bin/*' 2>/dev/null)
  printf '%s' "$best"
}

extract() { # <bin-name>
  local name="$1"
  [[ -f "$OUT/$name" ]] && return 0
  # macOS: search/copy from the cache volume via a throwaway container.
  if [[ -n "$VOL" ]]; then
    local p
    p="$(docker run --rm -v "$VOL":/cache alpine \
          sh -c "find /cache -type f -name '$name' -path '*/bin/*' 2>/dev/null | xargs -r ls -S 2>/dev/null | head -1 || true")"
    if [[ -n "$p" ]]; then
      docker run --rm -v "$VOL":/cache -v "$OUT":/out alpine \
        sh -c "cp '$p' /out/$name && chmod +x /out/$name"
      return 0
    fi
  fi
  # Linux: search the on-disk bazel cache directly.
  local hp
  hp="$(pick_largest "$HOST_CACHE" "$name")"
  [[ -n "$hp" ]] || { echo "ERROR: $name not found in bazel cache (vol=$VOL dir=$HOST_CACHE)"; exit 1; }
  cp "$hp" "$OUT/$name" && chmod +x "$OUT/$name"
}
extract runsc
extract containerd-shim-runsc-v1

echo "== artifacts (ARCH=$ARCH) =="
( cd "$OUT" && sha256sum runsc containerd-shim-runsc-v1 && file runsc containerd-shim-runsc-v1 )
echo "pinned gVisor commit: $(git -C "$GVISOR_SRC" rev-parse HEAD)"
