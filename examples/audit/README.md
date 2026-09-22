# POC: non-bypassable gVisor-level audit (seccheck), config-only

## What this proves

sandboxd only audits what goes through its API; a process the workload spawns
directly, or a raw `open`/`connect` it makes, is invisible to it. gVisor's Sentry
intercepts every guest syscall, so auditing there cannot be bypassed by the
workload — even as root inside the container.

This POC enables gVisor's upstream **seccheck** trace framework with **no fork of
runsc or the shim**, and answers the one risky question for our platform: does the
audit session survive checkpoint/restore?

## How it works (all upstream gVisor features)

- A trace session (`pod_init.json`) enables audit-relevant points — `container/start`,
  `sentry/execve`, `sentry/clone`, `syscall/openat/enter`, `syscall/connect/enter` —
  and a `remote` sink pointed at a Unix socket.
- runsc is told to load it via the `--pod-init-config` flag. We set that flag purely
  through the node's runsc runtime TOML (`[runsc_config]` in `runsc.toml`, the same
  table already used for `debug`/`debug-log`) — see `runsc.toml.snippet`. No source
  change to runsc or the containerd shim.
- The external auditor is the upstream `tracereplay save` tool listening on the
  socket. It runs on the node, outside every sandbox; the Sentry dials it. The
  workload has no access to that socket, so it cannot tamper with or silence audit.

Because `--pod-init-config` is a boot-time flag, `runsc restore` re-applies it — the
hypothesis being that a restored sandbox re-dials the sink. The POC verifies that
empirically.

## Run it (on the kind node)

1. Build the upstream `tracereplay` tool from the same gVisor tree runsc is built
   from (`bazel build //tools/tracereplay/main`) and install it on the node PATH.
2. Append `runsc.toml.snippet` to the node's runsc runtime TOML (e.g.
   `/etc/containerd/runsc.toml`). New pods pick it up on create; no containerd
   restart needed (the shim reads the TOML per container).
3. Run the verifier:

   ```
   scripts/verify-audit.sh <node-container> [kube-context]
   ```

It stages the config, starts `tracereplay save`, runs a source pod, generates
execve/openat/connect, checkpoints and forks/restores, regenerates syscalls in the
restored sandbox, and reports whether a second auditor client connected.

## The result to look for

`PASS` = the restored sandbox connected to the auditor as a new client, so audit
survives restore. `FAIL/FINDING` = no new client after restore, meaning restore
does not re-establish the session and audit would be silently lost — the outcome
that would change the design.

## Two decisions this POC surfaces

- **Fail-open vs fail-closed.** The sink config uses `ignore_setup_error: false`, so
  if the auditor is down at pod create, create fails (fail-closed). seccheck's steady
  state is drop-not-block: if the auditor stalls, points are dropped after backoff and
  the workload keeps running. "Audit can never be missed" and "audit never blocks the
  workload" are in tension — pick per compliance need, alert on `DroppedCount`.
- **Restore survival** is exactly what `verify-audit.sh` tests; treat its result as
  the gate for productionizing this.

## Relationship to sandboxd audit

This is the tamper-proof floor (ground-truth syscalls, no attribution). sandboxd
stays the attributable layer (credential, tool, tenant, reconstructed pty command).
The correlation spine on sandboxd audit joins them: seccheck events carry
`container_id`, which maps to `sandbox_id` via pod metadata.
