#!/usr/bin/env bash
# End-to-end pod fork: checkpoint a running gVisor pod (whole sandbox), then
# create two new pods that restore from the checkpoint. All three should report
# the SAME startup UUID (restored memory) with independent counters.
#
# Assumes the source pod (examples/pod-source.yaml) and forks
# (examples/pod-fork.yaml) use the `counter` workload writing state to a
# hostPath, and that the patched shim + annotation passthrough are installed.
#
# Usage: scripts/verify-fork.sh <node-container> [kube-context]
set -euo pipefail

NODE="${1:?usage: verify-fork.sh <node-container> [kube-context]}"
CTX="${2:-kind-gvisor-poc}"
ENDPOINT="unix:///run/containerd/containerd.sock"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
K() { kubectl --context "$CTX" "$@"; }

K delete pod counter counter-fork counter-fork2 --ignore-not-found --wait=true >/dev/null 2>&1 || true
docker exec "$NODE" bash -c 'rm -f /poc/state/* /poc/stateB/* /poc/stateC/* 2>/dev/null; mkdir -p /poc/state /poc/stateB /poc/stateC; true'

K apply -f "$HERE/examples/pod-source.yaml" >/dev/null
K wait --for=condition=Ready pod/counter --timeout=60s >/dev/null
sleep 6

CTR="$(docker exec "$NODE" crictl --runtime-endpoint "$ENDPOINT" ps -q --state Running --name counter | head -1)"
echo "source pod A:"; docker exec "$NODE" cat /poc/state/counter.state

echo "== whole-sandbox checkpoint =="
docker exec "$NODE" bash -c "rm -rf /poc/forkA && mkdir -p /poc/forkA && \
  ctr --address /run/containerd/containerd.sock -n k8s.io tasks checkpoint --image-path /poc/forkA $CTR >/dev/null 2>&1 && echo CHECKPOINT_OK"

echo "== fork two new pods from the checkpoint =="
K apply -f "$HERE/examples/pod-fork.yaml" >/dev/null
sed 's/counter-fork/counter-fork2/; s#/poc/stateB#/poc/stateC#' "$HERE/examples/pod-fork.yaml" | K apply -f - >/dev/null
sleep 10

echo "== result (same uuid => restored memory; diverging counters => independent) =="
echo -n "pod A  : "; docker exec "$NODE" cat /poc/state/counter.state
echo -n "fork B : "; docker exec "$NODE" cat /poc/stateB/counter.state
echo -n "fork C : "; docker exec "$NODE" cat /poc/stateC/counter.state
K get pods --no-headers | grep counter
