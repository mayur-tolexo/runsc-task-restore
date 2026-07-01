# Pod-shared disk-backed /workspace overlay + multi-container checkpoint/restore

This extends the checkpoint/restore work with a `/workspace` that two containers
in a pod share on **disk**, that `runsc checkpoint` captures, and that restores
cleanly for **every** container in the pod — not just the first.

Two changes make it work (both on the `neev/workspace-overlay` branch of the
gVisor fork, built by the [`build-fork`](../.github/workflows/build-fork.yml)
workflow):

1. **Shim** (`pkg/shim/v1/utils/volumes.go`): when an `emptyDir` carries
   `share=pod`, keep its OCI mount a `bind` (so the overlay upper is a disk
   filestore) and set the hint `type=tmpfs`. gVisor then builds one shared
   SelfOverlay master for the pod instead of a memory tmpfs or a gofer bind.
2. **runsc restore** (`runsc/boot/vfs.go`, `configureRestore`): a pod-shared
   overlay is one private MemoryFile owned by the first container; peers reuse it
   via `getSharedMount`. Restore was registering a MemoryFile per container, so a
   two-container pod restored with more MemoryFiles than the checkpoint saved and
   aborted with `inconsistent private memory files on restore`. `configureRestore`
   now mirrors `getSharedMount` — only the first-seen source registers a
   MemoryFile; peers close their extra filestore FD and skip.

## Environment

kind `kindest/node:v1.31.0` used as a privileged Linux host, gVisor nested,
linux/arm64. runsc/shim built from `neev/workspace-overlay`:

```
$ docker exec gvisor-poc-control-plane runsc --version
runsc version release-20260615.0-66-g0f3a32472c9d

$ kubectl exec gvisor-sanity -- dmesg | grep -i gvisor
[   0.000000] Starting gVisor...
```

Workload: the static Go `counter` (busybox base so `kubectl exec`/`runsc exec`
have `cat`/`ls`), one instance per container, each writing its state to
`/workspace/<name>.state` every second.

## 1. Two containers share one disk-backed /workspace

Deploy [`examples/ws-shared-pod.yaml`](../examples/ws-shared-pod.yaml) (volume
`workspace`, both containers mount it, pod carries
`dev.gvisor.spec.mount.workspace.{type=bind, share=pod}`). Each container sees
both containers' files:

```
-- writer-a /workspace --
a.state
b.state
-- writer-b /workspace --
a.state
b.state
```

The sandbox OCI spec carries the resolved hints, and the overlay upper is a
1&nbsp;GiB disk filestore in the emptyDir dir on the node — not RAM:

```
dev.gvisor.spec.mount.workspace.share":"pod"
dev.gvisor.spec.mount.workspace.source":"/var/lib/kubelet/pods/dc5a3b10-.../volumes/kubernetes.io~empty-dir/workspace"
dev.gvisor.spec.mount.workspace.type":"tmpfs"

$ ls -la .../kubernetes.io~empty-dir/workspace/
-rw-r--r-- 1 root root 1073741824 .gvisor.filestore.94f034a1aff795cf33d459af13b8fcad6f70aac03d0cae20577add2c58642f64
```

## 2. runsc checkpoint captures the live pod

```
$ runsc --root /run/containerd/runsc/k8s.io checkpoint --leave-running \
    --image-path /poc/ws-live <sandbox-id>
rc=0
$ ls /poc/ws-live
checkpoint.img  pages.img  pages_meta.img
```

State at checkpoint (both counters at 5):

```
a: {"uuid":"852b70a6-8acf-4a89-a01d-f620ce3ab1fa","start":"2026-07-01T07:39:22Z","counter":5}
b: {"uuid":"d536408a-327b-451d-88cd-65b006d4fdfa","start":"2026-07-01T07:39:22Z","counter":5}
```

## 3. Multi-container restore — before vs after the runsc fix

Delete the source pod (its emptyDir + filestore go away, so restore gets a
**fresh** workspace) and apply
[`examples/ws-shared-restore.yaml`](../examples/ws-shared-restore.yaml)
(`dev.gvisor.internal.restore.host-image-path: /poc/ws-live`).

**Before the fix** — `writer-a` restores, `writer-b` dies at start:

```
Warning  Failed  kubelet  spec.containers{writer-b}: Error: failed to start
containerd task "writer-b": OCI runtime restore failed: starting container:
starting sub-container [/counter --tick=1s --state-file=/workspace/b.state]:
inconsistent private memory files on restore:
savedMFOwners = [writer-a:/ writer-a:/workspace writer-b:/],
mfmap = map[writer-a:/ ... writer-a:/workspace ... writer-b:/ ... writer-b:/workspace ...]
```

`savedMFOwners` has three entries (the shared `/workspace` is owned once, by
`writer-a`); `mfmap` has four (one per container per mount). Counts disagree.

**After the fix** — both containers restore and resume:

```
$ kubectl get pod ws-counter-restore -o jsonpath=...
writer-a={"running":{"startedAt":"2026-07-01T07:39:31Z"}}
writer-b={"running":{"startedAt":"2026-07-01T07:39:32Z"}}

# /workspace content is back on the FRESH emptyDir (captured in the image):
$ kubectl exec ws-counter-restore -c writer-a -- cat /workspace/from-a.txt
hi-A

# both counters resume from 5 with the SAME startup UUIDs (no restart):
a: {"uuid":"852b70a6-8acf-4a89-a01d-f620ce3ab1fa","start":"2026-07-01T07:39:22Z","counter":11}
b: {"uuid":"d536408a-327b-451d-88cd-65b006d4fdfa","start":"2026-07-01T07:39:22Z","counter":10}
```

## 4. The sidecar can still write after restore

`writer-b` reuses the master's MemoryFile after restore. Writes from it land in
the shared overlay and stay coherent with the master both ways:

```
# sidecar (writer-b) writes:
$ kubectl exec ws-counter-restore -c writer-b -- sh -c 'echo write-from-sidecar-B-post-restore > /workspace/from-b.txt'
# master (writer-a) reads it:
$ kubectl exec ws-counter-restore -c writer-a -- cat /workspace/from-b.txt
write-from-sidecar-B-post-restore
# reverse — master writes, sidecar reads:
$ kubectl exec ws-counter-restore -c writer-b -- cat /workspace/from-a2.txt
write-from-master-A-post-restore
# sidecar's own state file keeps advancing:
b.state t0: {"uuid":"d536408a-...","counter":141}
b.state t1: {"uuid":"d536408a-...","counter":143}
# both containers' files coexist in the one shared dir:
$ kubectl exec ws-counter-restore -c writer-a -- ls /workspace
a.state  b.state  from-a.txt  from-a2.txt  from-b.txt
```

## Single-container overlay — for reference

The same overlay mounted by one container was the isolation check: it
checkpoints and restores cleanly on both the old and fixed runsc, which is what
pinned the bug to the multi-container duplicate rather than the overlay itself.

```
pre-checkpoint: {"uuid":"9a59c9a8-697f-4d07-9c3f-3edc5952bd6d",...,"counter":52}
# after delete + annotation-restore to a fresh pod:
restored /workspace: a.state  mark.txt
marker: single-marker
post:  {"uuid":"9a59c9a8-...","counter":58}     # same UUID, continued
+3s:   {"uuid":"9a59c9a8-...","counter":181}    # still climbing
```

## Direct runsc checkpoint/restore

The same live sandbox can be driven with `runsc` directly. `runsc checkpoint
--leave-running` produces the image above; `runsc restore --image-path ...
--bundle <copied-bundle> <new-id>` forks it into a second running sandbox
(version must match across checkpoint/restore — a sandbox started under one
runsc build cannot be restored by another).
