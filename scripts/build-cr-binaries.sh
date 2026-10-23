#!/usr/bin/env bash
# Build the patched shim, the matching runsc and its gvisor-bin/ sidecars from a
# gVisor checkout. They ship as a set: the shim shells out to runsc, and runsc
# refuses sidecars from another build. Extracts from the bazel cache because
# `make copy` trips on a long xargs line.
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

echo "== building //:release for ARCH=$ARCH (host=$host_arch) =="
# //:release is the installed layout: runsc, the shim and gvisor-bin/.
# Native builds leave ARCH unset: gVisor passes it to docker --platform, which
# rejects the aarch64 spelling.
make_args=(build TARGETS=//:release)
[[ "$ARCH" == "$host_arch" ]] || make_args+=(ARCH="$ARCH")
# The Makefile's post-build step fails on macOS bash, so trust bazel's verdict.
make "${make_args[@]}" 2>&1 | tee "$OUT/.build.log" || true
grep -q 'Target //:release up-to-date' "$OUT/.build.log" || { echo "ERROR: //:release did not build"; exit 1; }
rm -f "$OUT/.build.log"

# macOS keeps the bazel cache in Docker volumes, Linux on the host. Either may
# hold older builds, so take the newest release/runsc.
case "$ARCH" in x86_64) VOL_ARCH=amd64 ;; aarch64) VOL_ARCH=arm64 ;; esac
HOST_CACHE="${BAZEL_CACHE:-$HOME/.cache/bazel}"
best=""
for vol in $(docker volume ls --format '{{.Name}}' | grep "^gvisor-bazel-cache.*-$VOL_ARCH$" || true); do
  hit="$(docker run --rm -v "$vol":/cache alpine sh -c \
    'find /cache -type f -path "*/bin/release/runsc" -exec stat -c "%Y %n" {} +' || true)"
  [[ -n "$hit" ]] && best+="$(printf '%s\n' "$hit" | sed "s|\$| $vol|")"$'\n'
done
if [[ -n "$best" ]]; then
  read -r _ runsc vol < <(printf '%s' "$best" | sort -n | tail -1)
  docker run --rm -v "$vol":/cache -v "$OUT":/out alpine \
    sh -c "cp -rL '$(dirname "$runsc")'/. /out/ && chmod -R a+rx /out"
else
  runsc="$(find "$HOST_CACHE" -type f -path '*/bin/release/runsc' -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)"
  [[ -n "$runsc" ]] || { echo "ERROR: release/runsc not found under $HOST_CACHE"; exit 1; }
  cp -rL "$(dirname "$runsc")"/. "$OUT"/
fi
for f in runsc containerd-shim-runsc-v1 gvisor-bin/gvisor_sentry; do
  [[ -x "$OUT/$f" ]] || { echo "ERROR: $OUT/$f missing from the release layout"; exit 1; }
done

echo "== artifacts (ARCH=$ARCH) =="
( cd "$OUT" && find . -type f | sort | xargs sha256sum && file runsc containerd-shim-runsc-v1 )
echo "pinned gVisor commit: $(git -C "$GVISOR_SRC" rev-parse HEAD)"
