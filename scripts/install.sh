#!/usr/bin/env bash
# Install the patched shim into a node (a kind node container, by default) and
# restart containerd. Also enables passthrough of dev.neevcloud.* annotations
# to the container OCI spec so the restore trigger is reachable.
#
# Usage: scripts/install.sh <node-container> [/path/to/containerd-shim-runsc-v1]
set -euo pipefail

NODE="${1:?usage: install.sh <node-container> [shim-binary]}"
BIN="${2:-$(pwd)/out/containerd-shim-runsc-v1}"
[[ -f "$BIN" ]] || { echo "shim binary not found: $BIN"; exit 1; }

docker cp "$BIN" "$NODE:/usr/local/bin/containerd-shim-runsc-v1"
docker exec "$NODE" chmod +x /usr/local/bin/containerd-shim-runsc-v1

# Enable annotation passthrough on the runsc runtime (idempotent).
docker exec "$NODE" python3 - <<'PY'
p = "/etc/containerd/config.toml"
s = open(p).read()
if "pod_annotations" not in s:
    marker = '  runtime_type = "io.containerd.runsc.v1"\n'
    add = marker + '  pod_annotations = ["dev.neevcloud.*"]\n  container_annotations = ["dev.neevcloud.*"]\n'
    s = s.replace(marker, add, 1)
    open(p, "w").write(s)
    print("annotation passthrough added")
else:
    print("annotation passthrough already present")
PY

docker exec "$NODE" systemctl restart containerd
sleep 4
docker exec "$NODE" systemctl is-active containerd
docker exec "$NODE" sha256sum /usr/local/bin/containerd-shim-runsc-v1
echo "installed."
