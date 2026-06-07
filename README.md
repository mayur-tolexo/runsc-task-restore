# runsc-task-restore

**Working pod-level checkpoint, restore, and fork for gVisor (runsc) sandboxes on
Kubernetes**, by patching the `containerd-shim-runsc-v1` shim.

gVisor's `runsc` runtime can checkpoint and restore a sandbox, but that power is
not exposed through the normal orchestration stack: `kubectl`/kubelet only offer
a CRIU-based checkpoint (which cannot snapshot a gVisor sandbox), and the runsc
containerd shim returns `ErrNotImplemented` for the task `Checkpoint` RPC. This
repo closes the gap at the **shim** layer and demonstrates **forking a running
Kubernetes pod** — snapshotting one pod and booting N new pods that resume its
exact in-memory state.

## What this changes

Self-contained changes to `pkg/shim/v1` (see
[`patches/`](patches/0001-shim-task-checkpoint-and-annotation-restore.patch) and
[`changes/`](changes/)):

1. **Implement the shim `Checkpoint` task method** (was `ErrNotImplemented`).
   Shells out to `runsc checkpoint --leave-running`, so `ctr tasks checkpoint`
   (and any containerd client) drives gVisor's native, sandbox-wide checkpoint
   while the source pod keeps running.
2. **Whole-sandbox annotation-triggered restore.** A pod-wide annotation
   (`dev.neevcloud.restore-image-path`) makes a normal `Start` route into
   gVisor's existing restore path for **every** container in the pod — the
   `pause` root first (`runsc restore` → sentry enters the restoring state),
   then each sub-container (`RestoreSubcontainer`). The sentry resumes once
   `container_count` (recorded in the checkpoint metadata) containers are
   restored.

gVisor remaps checkpoint container IDs to the new pod's IDs **by container
name**; Kubernetes reuses the same container names across pods, so a fork needs
no ID rewriting.

## Status — verified end-to-end on a kind cluster (arm64)

| Capability | Result |
|---|---|
| Build patched shim (Bazel) | ✅ `containerd-shim-runsc-v1`, linux/arm64 |
| `ctr tasks checkpoint` on a gVisor pod | ✅ works (was `not implemented`); pod stays running |
| Restore a whole pod sandbox from a checkpoint | ✅ root + sub-containers restored as a unit |
| **Fork a new pod** from a checkpoint | ✅ new pod boots with the source's exact memory |
| **One-to-many fork** | ✅ N pods from one checkpoint, each independent |

**Demonstrated fork:** a source pod's counter process (random startup UUID +
incrementing counter) was checkpointed; two new pods were created from that
checkpoint and came up reporting the **same UUID and continuing the counter**,
then diverged independently:

| Pod | UUID | Counter | Role |
|---|---|---|---|
| `counter` | `a1cc1aa8…` | 50 | source (kept running) |
| `counter-fork` | `a1cc1aa8…` | 41 | fork — independent |
| `counter-fork2` | `a1cc1aa8…` | 16 | fork — independent |

See [VERIFICATION.md](VERIFICATION.md) for the full run.

## How it works (one paragraph)

A Kubernetes pod is a single gVisor sandbox hosting the `pause` (root) container
plus the app/sidecar sub-containers. `runsc checkpoint` is sandbox-wide and
records the container count. To fork, the new pod is created normally but every
container carries the restore annotation: the shim runs `runsc restore` instead
of cold start. gVisor's loader rebuilds the sentry from the image, **remaps the
checkpointed container IDs to the new pod's IDs by container name**, and resumes
once all containers are restored. Full design in [docs/HLD.md](docs/HLD.md);
diagrams in [docs/flows.md](docs/flows.md).

## Layout

- **[Release `gvisor-cr-pr13326`](https://github.com/mayur-tolexo/runsc-task-restore/releases/tag/gvisor-cr-pr13326)** — prebuilt `runsc` + `containerd-shim-runsc-v1` (arm64; pinned gVisor `5a65ec1f`).
- [`NODE-SETUP.md`](NODE-SETUP.md) — what to configure on each sandbox node: install the binary pair from the release, containerd annotation passthrough, `runsc.toml`, the CephFS snapshot store, and a DaemonSet installer.
- [`SETUP.md`](SETUP.md) — how to run upstream PR #13326 on a real Kubernetes cluster: build the shim + runsc, distribute to nodes, containerd config, the fork workflow, and productionization.
- [`docs/walkthrough.md`](docs/walkthrough.md) — full layer-by-layer walkthrough: kubelet → container start, gVisor internals, containerd & runsc checkpoint/restore, plus deep dives (async page loading, gofer re-establishment, netstack/sockets). All diagrams validated with mermaid-cli.
- [`docs/HLD.md`](docs/HLD.md) — high-level design: layers, where the fix goes, the whole-sandbox restore state machine, ID-remap-by-name.
- [`docs/flows.md`](docs/flows.md) — flow + sequence diagrams (checkpoint, whole-sandbox restore, working pod fork, one-to-many).
- [`patches/`](patches/) — the diff against upstream gVisor.
- [`changes/`](changes/) — the four modified shim files, for reading.
- [`examples/`](examples/) — source and fork pod manifests.
- [`scripts/`](scripts/) — build, install, verify-checkpoint, verify-fork.
- [`VERIFICATION.md`](VERIFICATION.md) — the empirical end-to-end run with real output.

## Quick start

```sh
scripts/build.sh   /path/to/gvisor        # build the patched shim (Bazel-in-Docker)
scripts/install.sh <node-container>       # install + enable annotation passthrough + restart containerd
scripts/verify-fork.sh <node-container>   # checkpoint a pod, fork two new pods, show same UUID
```

Built and verified against gVisor `release-20260601.0`, containerd v2.2.0,
Kubernetes v1.35 (kind), on linux/arm64.

## Limitations / next steps

- `Checkpoint` always uses `--leave-running` and ignores the request's `Options`
  (no `--exit`) — a deliberate fork-oriented default.
- Restore does not re-establish cgroup/OOM notifications (same TODO as the
  upstream cold-start path).
- Container **names** must match between source and forked pods (true for normal
  Kubernetes pods); gVisor's `dev.gvisor.container-name-remap.*` annotation
  handles the rename case if ever needed.
- Cross-node fork (shipping the image to another node) and snapshot
  storage/GC are out of scope for this POC.
