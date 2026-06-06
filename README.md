# runsc-task-restore

A POC that makes **gVisor (runsc) checkpoint/restore reachable from containerd**
for Kubernetes pods, by patching the `containerd-shim-runsc-v1` shim.

gVisor's `runsc` runtime can checkpoint and restore a sandbox, but that power is
not exposed through the normal orchestration stack: `kubectl`/kubelet only offer
a CRIU-based checkpoint (which cannot snapshot a gVisor sandbox), and the runsc
containerd shim returns `ErrNotImplemented` for the task `Checkpoint` RPC. This
repo closes that gap at the **shim** layer — the architecturally correct place —
and documents exactly how far pod-level snapshot/fork can go and where gVisor's
own invariants stop it.

## What this changes

Two small, self-contained changes to `pkg/shim/v1` (see
[`patches/`](patches/0001-shim-task-checkpoint-and-annotation-restore.patch) and
[`changes/`](changes/)):

1. **Implement the shim `Checkpoint` task method.** Mirrors the existing
   `Restore` wiring to shell out to `runsc checkpoint --leave-running`, so
   `ctr tasks checkpoint` (and any containerd client) drives gVisor's native
   checkpoint instead of failing with `not implemented`.
2. **Annotation-triggered restore.** The shim's `Restore` path already exists
   but isn't reachable over stock containerd. A new annotation
   (`dev.neevcloud.restore-image-path`, scoped to one container via
   `dev.neevcloud.restore-container`) makes a normal `Start` route into the
   existing restore path — so a *forked pod* can boot its app container from a
   checkpoint image.

## Status (verified on a kind cluster, arm64)

| Capability | Result |
|---|---|
| Build patched shim (Bazel) | ✅ `containerd-shim-runsc-v1`, linux/arm64 |
| `ctr tasks checkpoint` on a gVisor pod | ✅ **works** (was `not implemented`); pod stays running |
| Annotation-triggered restore reaches gVisor | ✅ shim routes `Start` → `runsc restore` |
| Fork a *new pod* from an app-container checkpoint | ⛔ blocked by gVisor invariant: `cannot restore subcontainer: sandbox is not being restored` |

**Key finding:** a Kubernetes pod is a *multi-container* gVisor sandbox
(`pause` + app + sidecars). gVisor will not restore a sub-container into a
freshly-started sandbox — the **whole sandbox must be restored as a unit**. So
true pod fork requires restoring the pod *sandbox* (pause) from a checkpoint and
then restoring each sub-container together. That next layer (the containerd
Sandbox service restore path) is described in [docs/HLD.md](docs/HLD.md).

## Layout

- [`docs/HLD.md`](docs/HLD.md) — high-level design: layers, where the fix goes, components, the whole-sandbox restore plan.
- [`docs/flows.md`](docs/flows.md) — flow + sequence diagrams (checkpoint, restore, pod-fork, the blocking invariant).
- [`patches/`](patches/) — the diff against upstream gVisor.
- [`changes/`](changes/) — the four modified shim files, for reading.
- [`scripts/`](scripts/) — build, install, and verify scripts.
- [`VERIFICATION.md`](VERIFICATION.md) — the empirical run with real command output.

## Quick start

```sh
# build the patched shim (needs the gVisor source + its Bazel/Docker flow)
scripts/build.sh /path/to/gvisor

# install into a node running containerd + gVisor
scripts/install.sh <node-container-or-host>

# verify checkpoint via containerd
scripts/verify-checkpoint.sh
```

Built and verified against gVisor `release-20260601.0`, containerd v2.2.0,
Kubernetes v1.35 (kind), on linux/arm64.
