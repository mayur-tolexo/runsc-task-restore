# Setup — running PR #13326 (gVisor pod checkpoint/restore) on a Kubernetes cluster

How to build, distribute, and enable gVisor checkpoint/restore/fork on real
clusters using upstream PR
[#13326](https://github.com/google/gvisor/pull/13326)
("shim/runsc: support containerd checkpoint restore").

> **Pinned commit:** `5a65ec1fcfbc45f637975e7fe3fa988d8d8bfa34`
> Validated end-to-end on kind, Kubernetes v1.35 / containerd v2.2.0, linux/arm64.

## Read first — caveats

1. **#13326 is not merged** (in active review). Pin to a commit; re-pin or drop
   the patch when it merges. The annotation contract can still change.
2. **gVisor rootfs overlay is required** for checkpoint/restore (per maintainer
   on #11810). Run the runsc runtime with the overlay enabled; verify on nodes.
3. **Cross-node fork needs the checkpoint image on the target node.**
   `host-image-path` is a node-local path — the forker must place/fetch it there.
4. **Test in staging first.** `hostinet` sockets and established TCP do not
   survive a checkpoint, GPU needs the `save-restore-exec` hook, and a sidecar
   (e.g. `sandboxd`) becomes part of the checkpointed sandbox and is restored
   too — validate it behaves after restore.

## 1. Build the shim and runsc (as a pair)

The shim shells out to `runsc checkpoint`/`runsc restore`, so build and ship
**both** binaries from the same gVisor commit and for your **node arch**
(`amd64` for most cloud nodes, `arm64` for Graviton/ARM).

```sh
git clone https://github.com/google/gvisor.git && cd gvisor
gh pr checkout 13326                 # or: git checkout 5a65ec1fcfbc45f637975e7fe3fa988d8d8bfa34

# build both targets via gVisor's bazel-in-Docker flow
make copy TARGETS=//shim:containerd-shim-runsc-v1 DESTINATION=./out
make copy TARGETS=//runsc:runsc                   DESTINATION=./out
```

`make copy` may fail with `xargs: command line cannot be assembled, too long` —
the **bazel build still succeeds**; extract the binaries from the bazel cache.
The helper script does build + extract for both:

```sh
# from this repo
scripts/build-cr-binaries.sh /path/to/gvisor ./out
# -> ./out/runsc  ./out/containerd-shim-runsc-v1  (+ sha256 + pinned commit)
```

**Cross-arch:** to build `amd64` binaries on an `arm64` workstation (or vice
versa), build inside a linux node of the target arch, or pass bazel a target
platform. Simplest in CI: run the build on a runner of the target arch and
publish `gvisor-cr-<commit>-<arch>.tar` containing both binaries.

## 2. Distribute to every sandbox node

Replace both binaries wherever your nodes get `runsc` today — a **gVisor
installer DaemonSet** or the **node image**:

```
/usr/local/bin/runsc                      <- matching runsc
/usr/local/bin/containerd-shim-runsc-v1   <- patched shim (#13326)
```

Only data-plane nodes that run sandboxes need them.

### Option A — manual / scripted per node (good for staging)

```sh
NODE_SSH=user@node1
scp ./out/runsc ./out/containerd-shim-runsc-v1 "$NODE_SSH":/tmp/
ssh "$NODE_SSH" 'sudo install -m0755 /tmp/runsc /usr/local/bin/runsc && \
                 sudo install -m0755 /tmp/containerd-shim-runsc-v1 /usr/local/bin/containerd-shim-runsc-v1'
# (kind dev node, for reference)
# docker cp ./out/runsc <node>:/usr/local/bin/runsc
# docker cp ./out/containerd-shim-runsc-v1 <node>:/usr/local/bin/containerd-shim-runsc-v1
```

### Option B — DaemonSet installer (production)

Ship the two binaries in an image and run a privileged DaemonSet that copies
them to the host and (re)writes the containerd config below, then restarts
containerd. Sketch:

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata: { name: gvisor-cr-installer, namespace: kube-system }
spec:
  selector: { matchLabels: { app: gvisor-cr-installer } }
  template:
    metadata: { labels: { app: gvisor-cr-installer } }
    spec:
      hostPID: true
      nodeSelector: { sandbox.neevcloud.com/runsc: "true" }   # only sandbox nodes
      containers:
        - name: installer
          image: <your-registry>/gvisor-cr-installer:<commit>-<arch>
          securityContext: { privileged: true }
          volumeMounts:
            - { name: hostbin, mountPath: /host/usr/local/bin }
            - { name: hostetc, mountPath: /host/etc/containerd }
          command: ["/bin/sh","-c"]
          args:
            - |
              install -m0755 /artifacts/runsc /host/usr/local/bin/runsc
              install -m0755 /artifacts/containerd-shim-runsc-v1 /host/usr/local/bin/containerd-shim-runsc-v1
              # merge the runsc runtime + annotation passthrough into the host config,
              # then restart containerd via nsenter on the host PID namespace
              sleep infinity
      volumes:
        - { name: hostbin, hostPath: { path: /usr/local/bin } }
        - { name: hostetc, hostPath: { path: /etc/containerd } }
```

Roll out node-by-node; **drain** a node before restarting containerd.

## 3. containerd config (on each sandbox node)

The annotation passthrough is the load-bearing piece — without it kubelet's
annotation never reaches the shim and the pod just cold-starts.

```toml
# /etc/containerd/config.toml
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
  pod_annotations       = ["dev.gvisor.*"]
  container_annotations = ["dev.gvisor.*"]
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
    TypeUrl    = "io.containerd.runsc.v1.options"
    ConfigPath = "/etc/containerd/runsc.toml"
```

```toml
# /etc/containerd/runsc.toml
[runsc_config]
  platform = "systrap"   # or "kvm" where nested virtualization is available
  # ensure gVisor rootfs overlay is enabled (required for checkpoint/restore)
```

```sh
sudo systemctl restart containerd      # drain the node first if it serves traffic
```

## 4. Verify on a node

```sh
# patched shim present?
sha256sum /usr/local/bin/containerd-shim-runsc-v1
grep -ac dev.gvisor.checkpoint.host-image-path /usr/local/bin/containerd-shim-runsc-v1   # expect 1

# checkpoint smoke test against a running runsc pod's container
CID=$(crictl --runtime-endpoint unix:///run/containerd/containerd.sock ps -q --state Running --name <ctr> | head -1)
ctr -n k8s.io tasks checkpoint --image-path /var/lib/gvisor-cr/test "$CID"   # succeeds; pod stays Running
```

## 5. Fork workflow at runtime

```
SNAPSHOT (source sandbox)
  1. find the sandbox's app container id:  crictl ps
  2. ctr -n k8s.io tasks checkpoint --image-path <imgdir> <id>   # whole-sandbox, --leave-running
  3. copy <imgdir> to durable storage (object store / shared volume)

FORK (new pod)
  4. ensure the image is present at <host-path> on the TARGET node
     (pre-fetch via an initContainer or node-local cache + node affinity)
  5. create the forked Pod with:
        metadata:
          annotations:
            dev.gvisor.checkpoint.host-image-path: <host-path>
  6. kubelet starts it normally -> shim restores the whole sandbox -> pod resumes
```

Annotation reference (set on `metadata.annotations` of the forked pod):

| Annotation | Meaning |
|---|---|
| `dev.gvisor.checkpoint.host-image-path` | node-local path to the checkpoint image dir; triggers restore on Start |
| `dev.gvisor.checkpoint.direct` | use direct IO for restore |
| `dev.gvisor.checkpoint.save-restore-exec-argv` | hook runsc runs inside the sandbox before save / after restore (e.g. `cuda-checkpoint`) |
| `dev.gvisor.checkpoint.save-restore-exec-timeout` | Go duration bounding that hook |

A minimal forked pod manifest:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-pod-fork
  annotations:
    dev.gvisor.checkpoint.host-image-path: /var/lib/gvisor-cr/snap-123
spec:
  runtimeClassName: gvisor          # mapped to the runsc handler
  restartPolicy: Never
  containers:
    - name: app                     # name must match the source pod's container
      image: <same image as source>
      # … same spec as the source container …
```

> Container **names** must match the source pod (gVisor remaps checkpoint
> container IDs to the new pod's IDs by name). Kubernetes reuses names, so this
> is automatic.

## 6. Productionize in aiagent-service

- **Snapshot** — a control-plane call that runs containerd task `Checkpoint` on
  the sandbox, pushes the image to a snapshot store, and records
  snapshot-id → image mapping.
- **Fork on create** — when creating a Sandbox CR `from_snapshot=<id>`, stamp
  the pod template (in the CR builder / `injectSandboxdSidecar`) with
  `dev.gvisor.checkpoint.host-image-path`, plus node affinity / a pre-fetch step
  so the image is local on the scheduled node.
- **sandboxd** — confirm the sidecar tolerates being restored (it is part of the
  sentry). Test PTY/exec endpoints post-restore.
- **GC/retention** — manage snapshot image lifetime in the snapshot store and on
  node caches.

## 7. Rollback

Keep the stock `runsc` + `containerd-shim-runsc-v1` in the node image. Rollback =
redeploy the previous installer/node image and restart containerd. The
annotation is inert on a stock shim (pods cold-start), so a partial rollout
degrades gracefully rather than breaking pods.

---

See [walkthrough.md](docs/walkthrough.md) for how the pieces fit, and
[VERIFICATION.md](VERIFICATION.md) for the end-to-end test this setup is based on.
