# Verification

Environment: kind v0.31 (Kubernetes v1.35, containerd v2.2.0), gVisor
`release-20260601.0`, Docker Desktop, Apple Silicon / **linux/arm64**. gVisor
platform `systrap` (no nested virtualization).

Workload: a static Go `counter` that mints a random **UUID** + start time at
boot and increments a counter every second, writing state to a hostPath file and
stdout. A faithful restore reproduces the **same UUID** (a cold start would mint
a new one) and a **continuing counter**.

## 1. Build the patched shim

`go build` cannot build the shim from the monorepo (Bazel proto codegen + split
packages), so gVisor's Bazel-in-Docker flow is used:

```
make copy TARGETS=//shim:containerd-shim-runsc-v1 DESTINATION=...
# first build: Build completed successfully, 2933 total actions
# incremental rebuild after edits: ~10s
```

Result: `containerd-shim-runsc-v1`, ELF 64-bit aarch64, static.

> `make copy`'s helper trips on `xargs: command line cannot be assembled, too
> long`; the bazel build itself succeeds and the artifact is extracted from the
> bazel cache (`scripts/build.sh` does this automatically).

## 2. Install

Patched shim → `/usr/local/bin/containerd-shim-runsc-v1`; enable annotation
passthrough on the runsc runtime (`pod_annotations = ["dev.neevcloud.*"]`);
`systemctl restart containerd`; node returns Ready.

## 3. Checkpoint via containerd — works (was `not implemented`)

```
$ ctr -n k8s.io tasks checkpoint --image-path /poc/forkA <agent-container-id>
CHECKPOINT_OK
$ ls /poc/forkA
checkpoint.img  pages.img  pages_meta.img
$ kubectl get pod counter
counter   1/1   Running   0   9s          # source stays running (--leave-running)
```

Before the patch: `ctr: not implemented`.

## 4. Fork a new pod — works

Source pod `counter` at checkpoint time:

```
{"uuid":"a1cc1aa8-9a53-4375-883e-ba42c6479e0e","start":"2026-06-06T12:42:54Z","counter":7}
```

Fork pod `counter-fork` (created with `dev.neevcloud.restore-image-path:
/poc/forkA`):

```
$ kubectl get pod counter-fork
counter-fork   1/1   Running   0   10s
$ cat /poc/stateB/counter.state
{"uuid":"a1cc1aa8-9a53-4375-883e-ba42c6479e0e","start":"2026-06-06T12:42:54Z","counter":17}
```

Same UUID and start time as the source → the whole sandbox was restored with its
in-memory state; the counter continued past the checkpoint value.

## 5. One-to-many fork + independence — works

A second fork (`counter-fork2`) from the same image, observed alongside the
source and first fork:

```
pod A  : {"uuid":"a1cc1aa8-…","start":"…12:42:54Z","counter":50}
fork B : {"uuid":"a1cc1aa8-…","start":"…12:42:54Z","counter":41}
fork C : {"uuid":"a1cc1aa8-…","start":"…12:42:54Z","counter":16}

NAME            READY   STATUS    RESTARTS   AGE
counter         1/1     Running   0          52s
counter-fork    1/1     Running   0          34s
counter-fork2   1/1     Running   0          10s
```

All three share the source's UUID + start time (same captured memory); counters
diverge (independent processes). Confirms one checkpoint → many independent
pods.

## Summary

| Step | Outcome |
|---|---|
| Build patched shim (arm64, Bazel) | ✅ |
| `ctr tasks checkpoint` on gVisor pod | ✅ works, source stays running |
| Restore whole pod sandbox (root + sub-containers) | ✅ |
| Fork a new pod from a checkpoint | ✅ same UUID, counter continues |
| One-to-many fork, independent | ✅ |

## Code review

The shim changes were reviewed by an independent agent; blocker and should-fix
findings were applied before the final verification:

- **Fixed (blocker):** `Init.Checkpoint` no longer holds `p.mu` across the
  blocking `runsc checkpoint` exec (would have risked stalling shim exit
  handling if the sandbox exited mid-checkpoint).
- **Fixed:** `Container.Checkpoint` guards `task == nil` and uses `c.task`
  directly (no type-assertion panic).
- **Fixed:** `Start` logs `ReadSpec` errors instead of silently cold-starting a
  container that should have restored.
- **Documented limitation:** `Checkpoint` always uses `--leave-running` and
  ignores the request `Options` (no `--exit`) — a deliberate fork default.
