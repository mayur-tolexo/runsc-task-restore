#!/usr/bin/env bash
# Verify gVisor-level, non-bypassable audit via the seccheck framework, and that
# the audit session SURVIVES checkpoint/restore.
#
# Approach is config-only (no runsc/shim fork): the node's runsc.toml sets
# --pod-init-config, which points runsc at a trace session with a `remote` sink;
# the upstream `tracereplay save` tool is the external auditor listening on a
# Unix socket. Because --pod-init-config is a boot-time flag re-applied on
# `runsc restore`, a restored sandbox should re-dial the sink as a NEW client.
#
# The key result: a SECOND tracereplay client file appears (and grows) after the
# fork/restore. That confirms audit is not lost across restore. If no second
# client connects, restore does NOT re-establish the session — the finding that
# would reshape the design.
#
# Usage: scripts/verify-audit.sh <node-container> [kube-context]
set -euo pipefail

NODE="${1:?usage: verify-audit.sh <node-container> [kube-context]}"
CTX="${2:-kind-gvisor-poc}"
ENDPOINT="unix:///run/containerd/containerd.sock"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
AUDIT_DIR="/run/gvisor-audit"
SOCK="$AUDIT_DIR/events.sock"
TRACE_OUT="$AUDIT_DIR/trace"
K() { kubectl --context "$CTX" "$@"; }
N() { docker exec "$NODE" bash -c "$1"; }

echo "== stage the trace-session config and audit dir on the node =="
docker cp "$HERE/examples/audit/pod_init.json" "$NODE:$AUDIT_DIR/pod_init.json" 2>/dev/null || {
  N "mkdir -p $AUDIT_DIR"; docker cp "$HERE/examples/audit/pod_init.json" "$NODE:$AUDIT_DIR/pod_init.json"; }
N "mkdir -p $TRACE_OUT && rm -f $TRACE_OUT/* $SOCK 2>/dev/null; true"

echo "== confirm runsc.toml enables pod-init-config (add the snippet if missing) =="
N "grep -q 'pod-init-config' /etc/containerd/runsc.toml 2>/dev/null && echo 'pod-init-config already set' || echo 'MISSING: append examples/audit/runsc.toml.snippet to /etc/containerd/runsc.toml'"

echo "== start the external auditor (tracereplay save) on the node =="
# tracereplay is an upstream gVisor tool (//tools/tracereplay/main); build it from
# the same gVisor tree runsc is built from and install it on the node PATH.
N "pkill -f 'tracereplay save' 2>/dev/null; true"
N "setsid tracereplay save --endpoint=$SOCK --out=$TRACE_OUT >$AUDIT_DIR/sink.log 2>&1 < /dev/null &"
sleep 1
N "test -S $SOCK && echo 'sink listening' || { echo 'sink socket absent'; cat $AUDIT_DIR/sink.log; exit 1; }"

echo "== source pod =="
K delete pod counter --ignore-not-found --wait=true >/dev/null 2>&1 || true
K apply -f "$HERE/examples/pod-source.yaml" >/dev/null
K wait --for=condition=Ready pod/counter --timeout=60s >/dev/null
sleep 3

echo "== generate ground-truth syscalls (execve + openat + connect) =="
K exec counter -- sh -c 'ls /etc >/dev/null; cat /etc/hostname >/dev/null; wget -T2 -q -O- http://example.com >/dev/null 2>&1 || true' || true
sleep 2

echo "-- capture BEFORE restore --"
N "ls -l $TRACE_OUT; for f in $TRACE_OUT/client-*; do echo \"\$f: \$(wc -c <\"\$f\") bytes\"; done"
BEFORE="$(N "ls $TRACE_OUT/client-* 2>/dev/null | wc -l")"
echo "client files before restore: $BEFORE"

echo "== whole-sandbox checkpoint + fork/restore =="
CTR="$(N "crictl --runtime-endpoint $ENDPOINT ps -q --state Running --name counter | head -1")"
N "rm -rf /poc/auditA && mkdir -p /poc/auditA && ctr --address /run/containerd/containerd.sock -n k8s.io tasks checkpoint --image-path /poc/auditA $CTR >/dev/null 2>&1 && echo CHECKPOINT_OK"
K apply -f "$HERE/examples/pod-fork.yaml" >/dev/null
K wait --for=condition=Ready pod/counter-fork --timeout=60s >/dev/null || true
sleep 3

echo "== generate syscalls in the RESTORED sandbox =="
K exec counter-fork -- sh -c 'ls /etc >/dev/null; cat /etc/hostname >/dev/null; wget -T2 -q -O- http://example.com >/dev/null 2>&1 || true' || true
sleep 2

echo "-- capture AFTER restore --"
N "for f in $TRACE_OUT/client-*; do echo \"\$f: \$(wc -c <\"\$f\") bytes\"; done"
AFTER="$(N "ls $TRACE_OUT/client-* 2>/dev/null | wc -l")"
echo "client files after restore: $AFTER"

echo
echo "==================== RESULT ===================="
if [ "$AFTER" -gt "$BEFORE" ]; then
  echo "PASS: restored sandbox connected to the auditor as a new client."
  echo "      => the seccheck audit session SURVIVES checkpoint/restore."
else
  echo "FAIL/FINDING: no new auditor client after restore."
  echo "      => restore did NOT re-establish the audit session; audit would be"
  echo "         silently lost after restore. This reshapes the design."
fi
echo "Inspect points with: docker exec $NODE tracereplay replay --file $TRACE_OUT/client-0001"
