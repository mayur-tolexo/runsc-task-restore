# High-Level Design — runsc-task-restore

## 1. Problem

We want **snapshot, restore, and fork** of gVisor-isolated Kubernetes pods
(agent sandboxes), preserving in-memory process state — the E2B/Cloudflare-style
capability. gVisor's `runsc` runtime already implements native checkpoint/
restore of a sandbox, but no orchestration layer exposes it:

- **kubelet** (`ContainerCheckpoint` CRI API) is hardwired to **CRIU** and has
  **no restore verb**. CRIU cannot checkpoint a gVisor sandbox.
- **containerd task API** (`ctr tasks checkpoint`) forwards to the runtime shim;
  the runsc shim returns **`ErrNotImplemented`**.
- **raw `runsc` CLI** works, but only for **single-container** sandboxes and
  with no integration into pod lifecycle.

## 2. Where the fix belongs

```mermaid
flowchart TD
    subgraph K8s["Kubernetes control plane"]
      kubelet["kubelet — ContainerCheckpoint (CRIU, no restore) ❌"]
    end
    subgraph Node["Node"]
      cd["containerd — task & sandbox services"]
      shim["containerd-shim-runsc-v1  ⟵ FIX HERE"]
      runsc["runsc (gVisor) — native checkpoint/restore ✅"]
    end
    kubelet -->|CRI| cd
    ctr["ctr / controller"] -->|task API| cd
    cd -->|ttrpc| shim
    shim -->|exec| runsc
    style shim fill:#ffe9b3,stroke:#d98c00,stroke-width:2px
    style runsc fill:#cce7cc,stroke:#2e8b2e
    style kubelet fill:#f8d0d0,stroke:#c0392b
```

The **shim** is the correct insertion point:

- It is gVisor's own adapter between containerd and `runsc`.
- It already knows the pod ↔ container topology (CRI annotations:
  `container-type`, `sandbox-id`, `container-name`).
- The `runsc` checkpoint/restore primitive underneath already works — only the
  shim wiring is missing.
- kubelet and containerd-core need **no changes**.

## 3. What already existed vs. what we added

The gVisor shim (`pkg/shim/v1`) already had a **`Restore`** path
(`extension.RestoreRequest{ImagePath}` → `Container.Restore` → `Init.start`
with a `RestoreConfig` → `runsc restore`). It was unreachable over stock
containerd (the task API has no `Restore` RPC; it's wired for gVisor's own
containerd integration). **Only `Checkpoint` was missing.**

| Component | Before | After (this POC) |
|---|---|---|
| `runsccmd.Runsc.Checkpoint` | absent | added — `runsc checkpoint --image-path … --leave-running` |
| `proc.Init.Checkpoint` | absent | added — locks + calls runtime |
| `runsc.Container.Checkpoint` | absent | added — uses `CheckpointTaskRequest.Path` |
| `runscService.Checkpoint` | `ErrNotImplemented` | implemented — looks up container, checkpoints |
| `runscService.Start` | cold start only | restore when restore annotations select the container |

## 4. Components and data flow

```mermaid
flowchart LR
    subgraph shimpkg["pkg/shim/v1"]
      svc["runsc/service.go\nCheckpoint() / Start()"]
      cont["runsc/container.go\nContainer.Checkpoint()"]
      init["proc/init.go\nInit.Checkpoint()"]
      cmd["runsccmd/runsc.go\nRunsc.Checkpoint() + CheckpointOpts"]
    end
    svc --> cont --> init --> cmd --> RUNSC[["runsc checkpoint / restore"]]
```

- **Checkpoint path:** `Checkpoint(CheckpointTaskRequest{ID, Path})` →
  `getContainer(ID)` → `Container.Checkpoint` → `Init.Checkpoint` →
  `Runsc.Checkpoint(id, {ImagePath: Path, LeaveRunning: true})`.
- **Restore path (annotation):** `Start` reads the container OCI spec; if
  `dev.neevcloud.restore-image-path` is set and this container is the named
  app container (`dev.neevcloud.restore-container`, never the sandbox/root), it
  calls the pre-existing `Container.Restore` with that image path.

## 5. Annotation contract

Pod-wide annotations reach every container's OCI spec via containerd runtime
passthrough (`pod_annotations = ["dev.neevcloud.*"]`). To avoid restoring the
`pause` and sidecar containers from the app's checkpoint, the shim restores a
container only when:

1. `dev.neevcloud.restore-image-path` is non-empty, **and**
2. the container is **not** the sandbox/root (`utils.IsSandbox` is false), **and**
3. its `io.kubernetes.cri.container-name` matches
   `dev.neevcloud.restore-container`.

## 6. The pod-fork invariant (why app-only restore is not enough)

A Kubernetes pod on gVisor is one sentry hosting multiple containers:

```mermaid
flowchart TD
    subgraph sandbox["gVisor sandbox (one sentry)"]
      pause["pause — sandbox/root container"]
      agent["agent — app sub-container"]
      sandboxd["sandboxd — sidecar sub-container"]
    end
    pause -. owns .-> agent
    pause -. owns .-> sandboxd
```

Restoring only the `agent` sub-container into a **freshly-started** sandbox is
rejected by gVisor:

```
cannot restore subcontainer: sandbox is not being restored, state=started
```

gVisor requires the **sandbox itself to be under restore** before any
sub-container can be restored into it. Checkpoint/restore is a **whole-sandbox**
operation.

## 7. Next layer — whole-sandbox restore (for true pod fork)

To fork a pod end to end, the design extends to the containerd **Sandbox**
service (`CreateSandbox`/`StartSandbox`), which the shim also serves:

```mermaid
sequenceDiagram
    participant Ctl as Fork controller
    participant CD as containerd
    participant Shim as runsc shim
    participant RS as runsc
    Note over Ctl: snapshot source pod
    Ctl->>CD: checkpoint sandbox (pause id) + each sub-container
    CD->>Shim: Checkpoint(...)
    Shim->>RS: runsc checkpoint --leave-running (whole sentry)
    Note over Ctl: fork → new pod
    Ctl->>CD: create sandbox FROM checkpoint (restore)
    CD->>Shim: StartSandbox(restore-image)
    Shim->>RS: runsc restore (sandbox/root)
    Ctl->>CD: start sub-containers FROM checkpoint
    CD->>Shim: Start(restore annotations)
    Shim->>RS: runsc restore (sub-containers into the restoring sandbox)
    RS-->>Ctl: pod resumes with preserved memory
```

Required work beyond this POC:

- A **restore variant of `StartSandbox`** so the pod's `pause`/sandbox is created
  via `runsc restore` (not started cold).
- **Identity regeneration** on restore: rewrite `linux.cgroupsPath` to the new
  pod's kubelet slice and point `io.kubernetes.cri.sandbox-id` at the new
  sandbox (the two things that broke hand-rolled raw-`runsc` restores).
- A **fork controller** (or the agent-sandbox controller) to: checkpoint the
  whole source sandbox, store the images, and stamp the new pod's CR/annotations
  so containerd drives the coordinated sandbox+sub-container restore.

## 8. Scope and non-goals

- In scope: shim `Checkpoint` implementation; annotation-driven restore trigger;
  empirical mapping of what works and what gVisor forbids.
- Out of scope (documented, not built): whole-sandbox restore via the Sandbox
  service; the fork controller; cross-node restore; storage/GC of images.
