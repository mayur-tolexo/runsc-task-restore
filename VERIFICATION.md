# Verification

Environment: kind v0.31 (Kubernetes v1.35, containerd v2.2.0), gVisor
`release-20260601.0`, Docker Desktop, Apple Silicon / **linux/arm64**. gVisor
platform `systrap` (no nested virtualization).

## 1. Build the patched shim

`go build` cannot build the shim from the monorepo (Bazel proto codegen + split
packages), so the gVisor Bazel-in-Docker flow is used:

```
make copy TARGETS=//shim:containerd-shim-runsc-v1 DESTINATION=...
# bazel: Build completed successfully, 2933 total actions  (first build)
# incremental rebuild after edits: ~10s
```

Result: `containerd-shim-runsc-v1`, ELF 64-bit aarch64, static. The patched
binary contains the new annotation strings (`dev.neevcloud.restore-image-path`,
`dev.neevcloud.restore-container`); the stock binary does not.

> Note: `make copy`'s helper trips on `xargs: command line cannot be assembled,
> too long`. The bazel build itself succeeds; the artifact is extracted directly
> from the bazel cache (`bazel-out/aarch64-fastbuild/bin/shim/...`).

## 2. Install + restart containerd

The patched shim replaces `/usr/local/bin/containerd-shim-runsc-v1` in the node;
`systemctl restart containerd`; node returns Ready.

## 3. Checkpoint via containerd — WORKS (was `not implemented`)

```
$ ctr -n k8s.io tasks checkpoint --image-path /poc/ckptA <agent-container-id>
exit=0
$ ls /poc/ckptA
checkpoint.img  pages.img  pages_meta.img
$ kubectl get pod counter
NAME      READY   STATUS    RESTARTS   AGE
counter   1/1     Running   0          6s        # pod stays running (--leave-running)
```

Before the patch the same command returned:

```
ctr: not implemented
```

## 4. Restore trigger — reaches gVisor's restore path

A forked pod (`counter-fork`) was created with:

```yaml
metadata:
  annotations:
    dev.neevcloud.restore-image-path: /poc/forkA
    dev.neevcloud.restore-container: counter
```

with containerd configured to pass `dev.neevcloud.*` annotations through to the
container OCI spec. The shim's `Start` correctly identified the app container as
the restore target (skipping `pause`) and invoked `runsc restore`.

## 5. Pod fork — blocked by a gVisor invariant (expected, documented)

```
$ kubectl describe pod counter-fork
  Reason:   StartError
  Message:  OCI runtime restore failed: starting container:
            starting sub-container [/counter ...]:
            sandbox is not being restored, cannot restore subcontainer: state=started
```

gVisor refuses to restore a sub-container into a sandbox that was started cold.
The pod's `pause`/sandbox must itself be restored from a checkpoint first. This
is the boundary between "task-level restore" (this POC) and "whole-sandbox
restore" (next layer — see [docs/HLD.md](docs/HLD.md) §7).

## Summary

| Step | Outcome |
|---|---|
| Build patched shim (arm64, Bazel) | ✅ |
| `ctr tasks checkpoint` on gVisor pod | ✅ works, pod stays running |
| Shim routes annotated `Start` → `runsc restore` | ✅ |
| App-only sub-container restore into fresh pod | ⛔ `sandbox is not being restored` |
| ⇒ requires whole-sandbox checkpoint+restore | documented as next layer |
