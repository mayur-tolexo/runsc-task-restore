# Checkpoint and restore a gVisor pod sandbox

Step-by-step for the two primitives the patched shim adds: **checkpoint** a
running gVisor sandbox, and **restore** it (in place or as a fork). This uses the
annotation contract from the **latest** PR
[#13326](https://github.com/google/gvisor/pull/13326) head, which uses the
`dev.gvisor.internal.*` keys — different from the earlier pinned commit.

## Annotation contract (latest #13326)

| Annotation | Side | Stripped before runsc | Meaning |
|---|---|---|---|
| `dev.gvisor.internal.restore.host-image-path` | restore | yes (shim-only) | absolute host dir holding `checkpoint.img`/`pages.img`; its presence makes the next `Start` dispatch to `runsc restore` for the whole sandbox |
| `dev.gvisor.internal.restore.direct` | restore | yes | `"true"` → `runsc restore --direct` |
| `dev.gvisor.internal.checkpoint.save-restore-exec-argv` | checkpoint | no (sentry reads it) | hook argv runsc execs in the sandbox around save/restore (e.g. `cuda-checkpoint`) |
| `dev.gvisor.internal.checkpoint.save-restore-exec-timeout` | checkpoint | no | Go duration bounding that hook (e.g. `10m`) |

> These replace the older `dev.gvisor.checkpoint.host-image-path` and
> `dev.neevcloud.restore-image-path` keys used in earlier docs and in
> `scripts/install.sh`. Match the annotation keys to the gVisor commit you pin.

## 1. Set up each sandbox node

Run on every data-plane node that runs gVisor sandboxes.

### 1a. Install the matched binary pair from the release

```sh
TAG=gvisor-cr-pr13326
REPO=mayur-tolexo/runsc-task-restore

# pick the asset suffix for this node's arch
case "$(uname -m)" in
  x86_64|amd64)  A=amd64 ;;
  aarch64|arm64) A=arm64 ;;
  *) echo "unsupported arch $(uname -m)"; exit 1 ;;
esac

# download runsc + shim + checksums for this arch
gh release download "$TAG" --repo "$REPO" -D /tmp/gvisor-cr \
  -p "runsc-linux-$A" -p "containerd-shim-runsc-v1-linux-$A" -p SHA256SUMS
# (no gh? use curl:)
# base=https://github.com/$REPO/releases/download/$TAG
# curl -fsSLO --output-dir /tmp/gvisor-cr $base/runsc-linux-$A
# curl -fsSLO --output-dir /tmp/gvisor-cr $base/containerd-shim-runsc-v1-linux-$A

# verify checksums, then install BOTH (they must be the same commit + arch)
( cd /tmp/gvisor-cr && grep -- "-linux-$A\$" SHA256SUMS | sha256sum -c - )
sudo install -m0755 "/tmp/gvisor-cr/runsc-linux-$A"                    /usr/local/bin/runsc
sudo install -m0755 "/tmp/gvisor-cr/containerd-shim-runsc-v1-linux-$A" /usr/local/bin/containerd-shim-runsc-v1
```

### 1b. Configure containerd

The annotation passthrough is load-bearing — without it kubelet's annotation
never reaches the shim and the pod just cold-starts. gVisor's rootfs **overlay
must be enabled** for checkpoint/restore.

```toml
# /etc/containerd/config.toml — runsc runtime
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type          = "io.containerd.runsc.v1"
  pod_annotations       = ["dev.gvisor.*"]
  container_annotations = ["dev.gvisor.*"]
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
    TypeUrl    = "io.containerd.runsc.v1.options"
    ConfigPath = "/etc/containerd/runsc.toml"
```

```toml
# /etc/containerd/runsc.toml
[runsc_config]
  platform = "systrap"     # or "kvm" where nested virt is available
  # ensure the gVisor rootfs overlay is enabled (required for C/R)
```

```sh
sudo systemctl restart containerd     # drain the node first if it serves traffic
```

### 1c. Verify the node

```sh
sha256sum /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1
# patched shim knows the restore annotation? expect a non-zero count:
grep -ac dev.gvisor.internal.restore.host-image-path /usr/local/bin/containerd-shim-runsc-v1
```

## 2. Checkpoint — snapshot a running sandbox

`runsc checkpoint` is whole-sandbox and always runs with `--leave-running` here,
so the source pod keeps running. Drive it through the containerd task
`Checkpoint` RPC (the shim implements it; stock runsc returned `not
implemented`).

```sh
CRI=unix:///run/containerd/containerd.sock

# any container of the target pod works — checkpoint captures the whole sandbox
CID=$(crictl --runtime-endpoint "$CRI" ps -q --state Running --name <app-container> | head -1)

IMG=/var/lib/gvisor-cr/snap-001
sudo mkdir -p "$IMG"
sudo ctr -n k8s.io tasks checkpoint --image-path "$IMG" "$CID"   # pod stays Running
ls -la "$IMG"      # expect checkpoint.img, pages.img, ...
```

If the restore runs on another node or later, copy `$IMG` to durable storage
(object store / shared volume) now, and place it back on the target node before
restoring.

GPU or other workloads that need an in-sandbox hook: set
`dev.gvisor.internal.checkpoint.save-restore-exec-argv` (and `…-timeout`) on the
**source** pod before checkpointing; the hook runs in the checkpointed
container's namespaces.

## 3. Restore — boot a sandbox from the snapshot

Restore is annotation-driven: create the pod normally but stamp the restore
annotation. The shim turns the standard `Start` RPC into `runsc restore` for the
whole sandbox (the `pause` root first, then each sub-container).

1. Ensure the image dir from step 2 is present at `<host-path>` on the **target
   node** (pre-fetch via an initContainer or a node-local cache + node affinity).
2. Apply the pod with the annotation and the runsc runtime class:

```sh
cat <<'YAML' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: my-pod-restored
  annotations:
    dev.gvisor.internal.restore.host-image-path: /var/lib/gvisor-cr/snap-001
    # dev.gvisor.internal.restore.direct: "true"   # optional: runsc restore --direct
spec:
  runtimeClassName: gvisor            # mapped to the runsc handler
  restartPolicy: Never
  containers:
    - name: <app-container>           # name MUST match the source pod's container
      image: <same image as source>
      # … same spec as the source container …
YAML

kubectl wait --for=condition=Ready pod/my-pod-restored --timeout=120s
```

Container **names** must match the source pod — gVisor remaps the checkpoint's
container IDs to the new pod's IDs **by name**. Kubernetes reuses names across
pods, so a fork needs no ID rewriting.

To restore N independent copies (one-to-many fork), apply N pods with distinct
`metadata.name` but the same `host-image-path`; each resumes the source's memory
and then diverges. See [SETUP.md](../SETUP.md) §5 for the full fork workflow and
[VERIFICATION.md](../VERIFICATION.md) for a real run.

### Node-local restore via ctr (kind dev node, no kubelet)

On a single node you can drive the same restore path directly: create the
container from the same bundle with the restore annotation in its OCI spec, then
start it — the shim dispatches `Start → runsc restore`.

```sh
# stamp the annotation into the bundle's config.json, then create + start
jq '.annotations["dev.gvisor.internal.restore.host-image-path"]="/var/lib/gvisor-cr/snap-001"' \
  bundle/config.json > bundle/config.json.tmp && mv bundle/config.json.tmp bundle/config.json
sudo ctr -n k8s.io containers create --runtime io.containerd.runsc.v1 <image> <ctr-id> # with that bundle
sudo ctr -n k8s.io tasks start <ctr-id>     # restores instead of cold-starting
```

## 4. Verify

```sh
crictl --runtime-endpoint "$CRI" ps --name <app-container>     # container Running
kubectl logs my-pod-restored                                   # resumes, not cold-start
```

- The restored pod reaches `Running` and **resumes in-memory state** rather than
  cold-starting (e.g. a counter/UUID process keeps its pre-checkpoint UUID and
  continues counting — the signal used in VERIFICATION.md).
- `runsc` debug logs show the restore path (`Start: dispatching to Restore
  (host-image-path=…)`).

## 5. Caveats

- **Whole-sandbox.** Checkpointing one container snapshots the entire pod
  sandbox; restoring brings the whole sandbox (including sidecars) back.
- **Networking.** `hostinet` sockets and established TCP connections do not
  survive checkpoint/restore.
- **GPU.** Needs the `save-restore-exec` hook (above); plain restore will not
  re-establish device state.
- **Annotation stripping.** The `restore.*` annotations are shim-only and
  stripped before runsc sees the spec; the `checkpoint.*` annotations are left in
  place because the sentry consumes them.
- **Cross-node restore** requires the checkpoint image present on the target node
  before the pod starts.
