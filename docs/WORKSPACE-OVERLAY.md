# Pod-shared disk-backed /workspace overlay + multi-container checkpoint/restore

This extends the checkpoint/restore work with a `/workspace` that two containers
in a pod share on **disk**, that `runsc checkpoint` captures, and that restores
cleanly for **every** container in the pod — not just the first.

**No patch of ours is needed for this on a current release.** Two upstream
pieces plus pod annotations do it:

1. **Shim** (`pkg/shim/v1/utils/volumes.go`): stock `UpdateVolumeAnnotations`
   already handles it. Annotate the volume `type=bind` + `share=pod` and the shim
   rewrites the *hint* to `tmpfs` while leaving the OCI mount a `bind`, so gVisor
   builds one shared SelfOverlay master whose upper is a disk filestore — rather
   than a memory tmpfs or a gofer bind. This is what GKE's admission controller
   stamps; see [#13595](https://github.com/google/gvisor/issues/13595). We first
   carried a shim patch here, which turned out to duplicate stock behaviour.
2. **runsc restore** (`runsc/boot/restore.go`, `runsc/boot/vfs.go`): a pod-shared
   overlay is one private MemoryFile owned by the first container; peers reuse it
   via `getSharedMount`. Restore registered a MemoryFile per container, so a
   two-container pod restored with more MemoryFiles than the checkpoint saved and
   aborted with `inconsistent private memory files on restore`. Fixed upstream in
   [#13608](https://github.com/google/gvisor/issues/13608) (commit `fc507be3`,
   first in `release-20260803.0`): a `sharedMfs` set threads through the
   per-container loop so only the first-seen hint registers a MemoryFile and peers
   close their extra FDs.

So the only local patch left in the build is PR #13326 itself (the checkpoint/
restore trigger, still open upstream).

## Environment

kind `kindest/node:v1.31.0` used as a privileged Linux host, gVisor nested,
linux/arm64. The run below predates the upstream fix and was made with the local
patches; it was re-verified on `release-20260810.0` + PR #13326 alone, with the
same result (see "Re-verified on stock" at the end). runsc/shim built from
`neev/workspace-overlay`:

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

## Re-verified on stock (`release-20260810.0` + PR #13326 only)

Same kind cluster, binaries built from `neev/pr13326-20260810` — no shim patch,
no restore patch. Pod annotations only, at the caps aiagent-service now emits
(4Gi rootfs + 6Gi workspace out of a 10Gi quota):

```
$ docker exec gvisor-poc-control-plane runsc --version
runsc version release-20260615.0-392-g92eebf2f03f8

# the shim resolved our bind hint to a shared tmpfs hint over a disk source
dev.gvisor.spec.mount.workspace.type":"tmpfs
dev.gvisor.spec.mount.workspace.share":"pod
dev.gvisor.spec.mount.workspace.source":"/var/lib/kubelet/pods/.../kubernetes.io~empty-dir/workspace
-rw-r--r-- 1 root root 1073741824 .gvisor.filestore.c8b920aa...

$ kubectl exec clean-src -c agent -- df -h / /workspace
none    4.0G   0   4.0G   0% /
none    6.0G   8.0K 6.0G   0% /workspace
```

Both containers share it, each overlay caps independently, and an over-cap write
is `ENOSPC` with the pod still Running (`restarts 0,0`, no `Evicted` event):

```
$ kubectl exec clean-src -c sandboxd -- sh -c 'echo x > /workspace/nope'
sh: write error: No space left on device
```

Checkpoint and restore of the two-container pod, with the second container now
restoring cleanly on stock runsc:

```
$ runsc --root /run/containerd/runsc/k8s.io checkpoint --leave-running --image-path /poc/clean <sandbox>
rc=0

# fresh pod carrying the same hints + dev.gvisor.internal.restore.host-image-path
agent=0 sandboxd=0            # restart counts
none    4.0G  4.0G  0 100% /
none    6.0G  6.0G  0 100% /workspace
md5 before=726ce8297085f1f4494afa9b7f53e7ff
md5 after =726ce8297085f1f4494afa9b7f53e7ff
```

The workspace comes back on a fresh `emptyDir` with contents intact, the caps are
still enforced, and a post-restore sidecar write is visible to the agent.
