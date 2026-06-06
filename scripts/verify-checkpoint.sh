#!/usr/bin/env bash
# Verify that `ctr tasks checkpoint` drives gVisor checkpoint through the patched
# shim (returns "not implemented" with the stock shim). Requires a running
# gVisor pod whose app container is named by $NAME.
#
# Usage: scripts/verify-checkpoint.sh <node-container> [container-name] [image-path]
set -euo pipefail

NODE="${1:?usage: verify-checkpoint.sh <node-container> [name] [image-path]}"
NAME="${2:-counter}"
IMG="${3:-/poc/ckptA}"
ENDPOINT="unix:///run/containerd/containerd.sock"

CTR="$(docker exec "$NODE" crictl --runtime-endpoint "$ENDPOINT" ps -q --state Running --name "$NAME" | head -1)"
[[ -n "$CTR" ]] || { echo "no running container named $NAME"; exit 1; }
echo "container: $CTR"

docker exec "$NODE" bash -c "rm -rf '$IMG' && mkdir -p '$IMG' && \
  ctr --address /run/containerd/containerd.sock -n k8s.io tasks checkpoint --image-path '$IMG' $CTR && \
  echo CHECKPOINT_OK && ls -la '$IMG'"
