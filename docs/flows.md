# Flow Diagrams — runsc-task-restore

All diagrams render on GitHub (Mermaid).

## 1. The three checkpoint paths (only the shim path reaches gVisor)

```mermaid
flowchart TD
    A["kubectl … /checkpoint"] --> B["kubelet ContainerCheckpoint"]
    B --> C["containerd CRI"]
    C --> D{"runtime?"}
    D -->|runc| E["CRIU dump ✅"]
    D -->|runsc| F["CRIU ❌ cannot dump gVisor"]

    G["ctr tasks checkpoint"] --> H["containerd task service"]
    H --> I["runsc shim.Checkpoint"]
    I -->|before| J["ErrNotImplemented ❌"]
    I -->|after this POC| K["runsc checkpoint ✅"]

    L["runsc checkpoint (CLI)"] --> M["gVisor native S/R ✅ single-container only"]

    style J fill:#f8d0d0,stroke:#c0392b
    style F fill:#f8d0d0,stroke:#c0392b
    style K fill:#cce7cc,stroke:#2e8b2e
    style E fill:#cce7cc,stroke:#2e8b2e
    style M fill:#cce7cc,stroke:#2e8b2e
```

## 2. Checkpoint flow (implemented)

```mermaid
sequenceDiagram
    autonumber
    participant Client as ctr / controller
    participant CD as containerd
    participant Shim as runsc shim (patched)
    participant RS as runsc
    participant GV as gVisor sentry

    Client->>CD: tasks checkpoint --image-path P <ctr>
    CD->>Shim: Checkpoint(CheckpointTaskRequest{ID, Path=P})
    Shim->>Shim: getContainer(ID) → Container.Checkpoint
    Shim->>RS: runsc checkpoint --image-path=P --leave-running ID
    RS->>GV: serialize sentry state → checkpoint.img / pages.img
    GV-->>RS: done (container left running)
    RS-->>Shim: exit 0
    Shim-->>CD: Empty
    CD-->>Client: ok ; pod stays Running
```

## 3. Restore trigger flow (implemented)

```mermaid
sequenceDiagram
    autonumber
    participant K as kubelet
    participant CD as containerd
    participant Shim as runsc shim (patched)
    participant RS as runsc

    K->>CD: RunPodSandbox (new pod)
    CD->>Shim: CreateSandbox / StartSandbox  (pause starts cold)
    K->>CD: CreateContainer(app) + StartContainer(app)
    CD->>Shim: Start(StartRequest{ID})
    Shim->>Shim: ReadSpec(bundle) → shouldRestore(spec)?
    alt restore-image-path set AND not sandbox AND container-name matches
        Shim->>RS: runsc restore --image-path=… --detach ID
    else
        Shim->>RS: runsc start ID  (cold)
    end
```

## 4. Pod fork attempt — and where gVisor blocks it

```mermaid
sequenceDiagram
    autonumber
    participant Ctl as operator
    participant PodA as Pod A (source)
    participant CD as containerd
    participant Shim as runsc shim
    participant RS as runsc

    Ctl->>PodA: run counter (uuid=A, counter climbs)
    Ctl->>CD: ctr tasks checkpoint agent@PodA → /poc/forkA
    CD->>Shim: Checkpoint
    Shim->>RS: runsc checkpoint --leave-running
    RS-->>Ctl: image written ✅ (Pod A still running)

    Ctl->>CD: create Pod B with restore annotations → /poc/forkA
    CD->>Shim: StartSandbox (Pod B pause starts COLD)
    CD->>Shim: Start(agent@PodB)
    Shim->>RS: runsc restore --image-path=/poc/forkA agent@PodB
    RS-->>Shim: ❌ "cannot restore subcontainer:\n sandbox is not being restored, state=started"
    Shim-->>CD: StartError
```

The sandbox (Pod B's `pause`) was started cold, so gVisor refuses to restore the
sub-container into it.

## 5. The fix for fork — whole-sandbox restore (next layer)

```mermaid
sequenceDiagram
    autonumber
    participant Ctl as fork controller
    participant CD as containerd
    participant Shim as runsc shim
    participant RS as runsc

    rect rgb(235,245,235)
    Note over Ctl,RS: snapshot — whole sandbox
    Ctl->>CD: checkpoint sandbox (pause) + sub-containers
    CD->>Shim: Checkpoint(...)
    Shim->>RS: runsc checkpoint --leave-running (entire sentry)
    end

    rect rgb(235,240,250)
    Note over Ctl,RS: fork — restore sandbox first, then children
    Ctl->>CD: create Pod B sandbox FROM checkpoint
    CD->>Shim: StartSandbox(restore-image)  ← NEW restore variant
    Shim->>RS: runsc restore (sandbox/root)
    Ctl->>CD: start sub-containers FROM checkpoint
    CD->>Shim: Start(restore annotations)
    Shim->>RS: runsc restore (sub-containers into restoring sandbox)
    RS-->>Ctl: Pod B resumes with Pod A's memory (uuid=A)
    end
```

## 6. Component map

```mermaid
flowchart LR
    subgraph repo["this repo (patch targets in gVisor pkg/shim/v1)"]
      direction TB
      svc["runsc/service.go\n• Checkpoint()\n• Start() + shouldRestore()"]
      cont["runsc/container.go\n• Container.Checkpoint()"]
      init["proc/init.go\n• Init.Checkpoint()"]
      cmd["runsccmd/runsc.go\n• CheckpointOpts\n• Runsc.Checkpoint()"]
    end
    svc --> cont --> init --> cmd --> runsc[["runsc binary"]]
```
