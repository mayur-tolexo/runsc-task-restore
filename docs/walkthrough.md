# Walkthrough — from kubelet to container start, then checkpoint/restore

A complete, layer-by-layer explanation of how a gVisor pod starts, how gVisor
manages it, and how checkpoint/restore (and fork) work — at the containerd shim
and inside `runsc`.

Contents:
1. [kubelet → container start](#1-kubelet--container-start)
2. [How gVisor manages the pod](#2-how-gvisor-manages-the-pod)
3. [How containerd does checkpoint/restore](#3-how-containerd-does-checkpointrestore)
4. [How runsc checkpoints](#4-how-runsc-checkpoints)
5. [How runsc restores (state machine)](#5-how-runsc-restores-state-machine)
6. [Container ID remap by name (why fork works)](#6-container-id-remap-by-name)
7. [Deep dives: page loading, gofer, netstack](#7-deep-dives)

---

## 1. kubelet → container start

A pod with `runtimeClassName: gvisor` is built incrementally: the `pause`
container starts first and *creates the sandbox*, then each app/sidecar
container is created as a sub-container that joins it.

```mermaid
sequenceDiagram
    autonumber
    participant K as kubelet
    participant CD as containerd (CRI plugin)
    participant Shim as containerd-shim-runsc-v1
    participant RS as runsc

    K->>CD: RunPodSandbox (gvisor RuntimeClass)
    CD->>Shim: Task Create (pause = root container)
    Shim->>RS: runsc run pause
    RS-->>Shim: SANDBOX created (sentry boots)
    K->>CD: CreateContainer (agent)
    CD->>Shim: Task Create (agent)
    Shim->>RS: runsc create agent
    K->>CD: StartContainer (agent)
    CD->>Shim: Task Start (agent)
    Shim->>RS: runsc start agent (joins existing sentry)
    Note over K,RS: repeat Create/Start for each sidecar (e.g. sandboxd)
```

Live proof — one pod shows two runsc entries sharing one sentry PID:

```mermaid
flowchart LR
    subgraph pod["one pod = one sentry (PID 5561)"]
      root["pause<br/>container-type=sandbox<br/>id b9d001d4"]
      sub["counter<br/>container-type=container<br/>container-name=counter<br/>sandbox-id=b9d001d4"]
    end
    root -. owns .-> sub
```

---

## 2. How gVisor manages the pod

`runsc` is an OCI runtime, but instead of running your process on the host
kernel it interposes a userspace kernel (the sentry). One pod = one sentry
hosting all containers.

```mermaid
flowchart TB
    subgraph sandbox["gVisor SANDBOX — one sentry process"]
      apps["pause + counter + sandboxd<br/>(all containers, one address space)"]
      sentry["SENTRY<br/>Go userspace kernel: syscalls, MM,<br/>task scheduling, netstack"]
      apps --> sentry
    end
    sentry -->|"traps syscalls"| plat["PLATFORM<br/>systrap / KVM / ptrace"]
    sentry -->|"file ops"| gofer["GOFER<br/>host file proxy (lisafs/9P)"]
    plat --> host["HOST LINUX KERNEL"]
    gofer --> host
    style sentry fill:#cce7cc,stroke:#2e8b2e
    style plat fill:#ffe9b3,stroke:#d98c00
    style gofer fill:#ffe9b3,stroke:#d98c00
```

- **Sentry** — the application kernel. Container syscalls never hit the host
  kernel directly; this is the isolation boundary. All container state lives
  here, which is what makes a single, sandbox-wide checkpoint possible.
- **Platform** — how syscalls are trapped. We use `systrap` (no nested
  virtualization, important when the node is itself a container).
- **Gofer** — a separate host process proxying filesystem access.

---

## 3. How containerd does checkpoint/restore

containerd never checkpoints directly — it forwards to the runtime shim. Only
one of the three entry points reaches gVisor's native mechanism.

```mermaid
flowchart TD
    A["kubectl …/checkpoint"] --> B["kubelet ContainerCheckpoint"] --> C["containerd CRI"] --> CRIU["CRIU — cannot dump a sentry ❌"]
    G["ctr tasks checkpoint"] --> H["containerd task service"] --> I["runsc shim.Checkpoint"]
    I --> J["stock: ErrNotImplemented ❌"]
    I --> K["patched: runsc checkpoint ✅"]
    L["runsc checkpoint CLI"] --> M["gVisor native S/R ✅"]
    style CRIU fill:#f8d0d0,stroke:#c0392b
    style J fill:#f8d0d0,stroke:#c0392b
    style K fill:#cce7cc,stroke:#2e8b2e
    style M fill:#cce7cc,stroke:#2e8b2e
```

The patch wires `shim.Checkpoint` (was `ErrNotImplemented`) down to the `runsc`
CLI, mirroring the existing restore plumbing:

```mermaid
flowchart LR
    a["shim.Checkpoint(req)"] --> b["getContainer(req.ID)"] --> c["Container.Checkpoint<br/>guard task != nil"] --> d["Init.Checkpoint<br/>no p.mu across exec"] --> e["Runsc.Checkpoint<br/>runsc checkpoint --leave-running"]
```

---

## 4. How runsc checkpoints

`runsc checkpoint <any-container-id>` serializes the entire sentry and writes
three files plus metadata. The metadata is what makes multi-container restore
possible.

```mermaid
flowchart LR
    sentry["SENTRY (whole pod)"] -->|serialize| img
    subgraph img["checkpoint image dir"]
      a["checkpoint.img<br/>sentry state: tasks, MM,<br/>FD tables, netstack"]
      b["pages.img<br/>guest memory pages"]
      c["pages_meta.img<br/>page index"]
      d["metadata<br/>container_count = N<br/>container_specs = name + OCI spec"]
    end
    style d fill:#ffe9b3,stroke:#d98c00
```

`runsc/boot/restore.go` writes the **whole-pod** count, so checkpointing any
container captures the entire sandbox:

```text
saveOpts.Metadata[ContainerCountKey] = strconv.Itoa(l.containerCount())
saveOpts.Metadata[ContainerSpecsKey] = specsStr
```

---

## 5. How runsc restores (state machine)

gVisor refuses to restore a sub-container into a cold-started sandbox
(`cannot restore subcontainer: sandbox is not being restored`). Restore is a
whole-sandbox unit:

```mermaid
stateDiagram-v2
    [*] --> created
    created --> restoringUnstarted: runsc restore ROOT<br/>Sandbox.Restore reads container_count
    restoringUnstarted --> restoringUnstarted: runsc restore SUB<br/>RestoreSubcontainer
    restoringUnstarted --> restored: restored count equals container_count<br/>onRestoreDone resumes kernel
    restored --> [*]
```

The shim drives this from a normal pod create — every container carries the
pod-wide restore annotation, and `Start` runs `runsc restore` instead of cold
start:

```mermaid
sequenceDiagram
    autonumber
    participant CD as containerd
    participant Shim as runsc shim (patched)
    participant RS as runsc / sentry

    CD->>Shim: Start(pause) with restore-image-path annotation
    Shim->>RS: runsc restore pause
    RS-->>Shim: state restoringUnstarted, total equals container_count
    CD->>Shim: Start(agent) with restore-image-path annotation
    Shim->>RS: runsc restore agent
    RS-->>Shim: RestoreSubcontainer, count reached, sentry resumes
```

---

## 6. Container ID remap by name

The source pod and the forked pod have different container IDs. On restore
gVisor remaps each task's checkpoint container ID to the new pod's ID by
container **name** (`runsc/boot/restore.go`), so forks need no rewriting because
Kubernetes reuses container names.

```mermaid
flowchart TD
    subgraph ckpt["checkpoint (pod A)"]
      ta["task CID=agentA<br/>name=counter"]
      tp["task CID=pauseA<br/>name=__no_name_0"]
    end
    subgraph mapb["pod B registers names to new CIDs"]
      m["counter to agentB<br/>__no_name_0 to pauseB"]
    end
    ta -->|"ContainerName(oldCid)=counter"| m
    tp -->|"name=__no_name_0"| m
    m -->|RestoreContainerMapping| done["tasks now run under pod B CIDs"]
    style m fill:#ffe9b3,stroke:#d98c00
```

For the rename case gVisor honors `dev.gvisor.container-name-remap.<id>` with a
`from=to` value (`runsc/specutils/specutils.go`).

---

## 7. Deep dives

### 7a. Asynchronous page loading

The sentry state file (`checkpoint.img`) is deserialized by `LoadFrom`, while
guest memory pages (`pages.img` + `pages_meta.img`) stream in **asynchronously**
via an `AsyncMFLoader` (`runsc/boot/restore.go`). With `--background`, page
loading continues after the restore call returns, so the container can start
running before every page is resident.

```mermaid
sequenceDiagram
    autonumber
    participant R as restorer
    participant MF as AsyncMFLoader
    participant K as kernel.LoadFrom
    R->>MF: KickoffPrivate (start streaming pages.img)
    R->>K: LoadFrom (deserialize sentry state)
    K-->>R: kernel rebuilt
    R->>MF: WaitMetadata (page index ready)
    Note over R,MF: with --background, remaining pages load lazily after resume
```

### 7b. Gofer re-establishment

The gofer is **not** restored from the image. On restore a fresh gofer process
is created (`createGoferProcess` in `runsc/container/container.go`) and its FDs
(`goferFiles`, `goferFilestores`) are handed to the restored sandbox, so the
rootfs and mounts are reconnected to the current host paths. This is why a
forked pod's filesystem mounts work even though the IDs changed.

```mermaid
flowchart LR
    restore["runsc restore (root or sub)"] --> gofer["createGoferProcess<br/>(new gofer per container)"]
    gofer --> fds["goferFiles + goferFilestores FDs"]
    fds --> sentry["passed into restored sentry<br/>rootfs and mounts reconnected"]
    style gofer fill:#ffe9b3,stroke:#d98c00
```

### 7c. Netstack and sockets

gVisor's in-sandbox network stack (netstack) is part of the serialized sentry
state, so its interfaces, routes, and listening sockets are restored
(`afterLoad` hooks rebuild TCP endpoint state in `pkg/tcpip`). Two caveats:

- Established TCP connections survive only if save-restore capability is set
  (`GetAllowConnectedOnSave`); otherwise they are dropped on save.
- Host-backed sockets (`hostinet`) reference host FDs and cannot be meaningfully
  carried across a checkpoint.

For the fork demo the workload holds no sockets, so this does not apply — but it
is the boundary to know for stateful network workloads.

```mermaid
flowchart TD
    chk["checkpoint"] --> ns["netstack endpoints, routes,<br/>listening sockets — serialized ✅"]
    chk --> est["established TCP — only if<br/>AllowConnectedOnSave ⚠️"]
    chk --> hostsock["hostinet sockets —<br/>host FDs, not portable ❌"]
    style ns fill:#cce7cc,stroke:#2e8b2e
    style est fill:#ffe9b3,stroke:#d98c00
    style hostsock fill:#f8d0d0,stroke:#c0392b
```

---

See [HLD.md](HLD.md) for the design rationale and [flows.md](flows.md) for the
end-to-end checkpoint/fork sequences. All diagrams in this repo are validated
with `@mermaid-js/mermaid-cli`.
