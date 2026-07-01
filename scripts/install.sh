#!/usr/bin/env bash
# Install the patched runsc + shim pair into a node (a kind node container, by
# default), register the runsc runtime with dev.gvisor.* annotation passthrough,
# and restart containerd. Both binaries matter: the /workspace overlay hint is in
# the shim, the multi-container restore fix is in runsc. The dev.gvisor.*
# passthrough is load-bearing — without it the mount hints
# (dev.gvisor.spec.mount.*) and the restore trigger
# (dev.gvisor.internal.restore.*) never reach the OCI spec and the pod cold-starts.
#
# Usage: scripts/install.sh <node-container> [runsc-binary] [shim-binary]
set -euo pipefail

NODE="${1:?usage: install.sh <node-container> [runsc-binary] [shim-binary]}"
RUNSC="${2:-$(pwd)/out/runsc}"
SHIM="${3:-$(pwd)/out/containerd-shim-runsc-v1}"
[[ -f "$RUNSC" ]] || { echo "runsc binary not found: $RUNSC"; exit 1; }
[[ -f "$SHIM"  ]] || { echo "shim binary not found: $SHIM"; exit 1; }

docker cp "$RUNSC" "$NODE:/usr/local/bin/runsc"
docker cp "$SHIM"  "$NODE:/usr/local/bin/containerd-shim-runsc-v1"
docker exec "$NODE" chmod +x /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1

# Register the runsc runtime + annotation passthrough (idempotent). dev.gvisor.*
# covers both the mount hints and the restore trigger; dev.neevcloud.* is kept
# for the older annotation contract.
docker exec "$NODE" python3 - <<'PY'
p = "/etc/containerd/config.toml"
s = open(p).read()
annos = ('pod_annotations = ["dev.gvisor.*", "dev.neevcloud.*"]\n'
         '  container_annotations = ["dev.gvisor.*", "dev.neevcloud.*"]')
if "dev.gvisor.*" in s:
    print("gVisor annotation passthrough already present")
elif '["dev.neevcloud.*"]' in s:
    # upgrade an earlier neevcloud-only passthrough in place
    s = s.replace('["dev.neevcloud.*"]', '["dev.gvisor.*", "dev.neevcloud.*"]')
    open(p, "w").write(s); print("extended existing passthrough with dev.gvisor.*")
elif "runtimes.runsc]" not in s:
    # no runsc runtime yet: append a minimal block with passthrough
    s += ('\n[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]\n'
          '  runtime_type = "io.containerd.runsc.v1"\n  ' + annos + '\n')
    open(p, "w").write(s); print("runsc runtime + gVisor annotation passthrough added")
else:
    # runsc runtime exists without passthrough: add it after the runtime_type line
    marker = '  runtime_type = "io.containerd.runsc.v1"\n'
    s = s.replace(marker, marker + '  ' + annos + '\n', 1)
    open(p, "w").write(s); print("gVisor annotation passthrough added to existing runsc runtime")
PY

docker exec "$NODE" systemctl restart containerd
sleep 4
docker exec "$NODE" systemctl is-active containerd
docker exec "$NODE" sha256sum /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1
echo "installed."
