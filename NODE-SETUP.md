# Per-node setup — gVisor checkpoint/restore (PR #13326)

What to configure on **each sandbox node** to enable full-state pause/resume and
fork. Binaries are published as a GitHub Release; this doc wires them in.

> Release: [`gvisor-cr-pr13326`](https://github.com/mayur-tolexo/runsc-task-restore/releases/tag/gvisor-cr-pr13326)
> (pinned gVisor commit `5a65ec1fcfbc45f637975e7fe3fa988d8d8bfa34`).
> arm64 assets are attached; build amd64 with `scripts/build-cr-binaries.sh` and
> upload as `*-linux-amd64`.

Each node needs five things:
1. the `runsc` + `containerd-shim-runsc-v1` pair (matched commit),
2. a containerd `runsc` runtime handler with `dev.gvisor.*` annotation passthrough,
3. a `runsc.toml` (platform + rootfs overlay),
4. the CephFS snapshot store visible at a stable host path,
5. a containerd restart.

Plus, once per cluster: the `gvisor` RuntimeClass.

---

## 0. Prerequisites

- containerd v2.x with the CRI plugin.
- A homogeneous node arch + CPU class (checkpoints are arch- and
  CPU-feature-specific). Pick the asset matching the node arch.
- CephFS RWX storage class (`neevai-ceph-fs`) for the shared snapshot store.

## 1. Install the binary pair

```sh
REL=https://github.com/mayur-tolexo/runsc-task-restore/releases/download/gvisor-cr-pr13326
ARCH=$(uname -m); case "$ARCH" in aarch64) A=arm64;; x86_64) A=amd64;; esac

curl -fsSL -o /tmp/runsc        "$REL/runsc-linux-$A"
curl -fsSL -o /tmp/shim         "$REL/containerd-shim-runsc-v1-linux-$A"
curl -fsSL -o /tmp/SHA256SUMS   "$REL/SHA256SUMS"

# verify checksums (rename to match the SHA256SUMS entries)
cp /tmp/runsc /tmp/runsc-linux-$A; cp /tmp/shim /tmp/containerd-shim-runsc-v1-linux-$A
( cd /tmp && sha256sum -c SHA256SUMS --ignore-missing )

sudo install -m0755 /tmp/runsc /usr/local/bin/runsc
sudo install -m0755 /tmp/shim  /usr/local/bin/containerd-shim-runsc-v1
```

> The shim execs the `runsc` CLI — always install **both from the same release**.

## 2. containerd runtime + annotation passthrough

`/etc/containerd/config.toml` — add (or confirm) the runsc runtime. The
`pod_annotations`/`container_annotations` lists are **required**: without them
the `dev.gvisor.checkpoint.*` restore annotation never reaches the shim and pods
just cold-start.

```toml
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
  pod_annotations       = ["dev.gvisor.*"]
  container_annotations = ["dev.gvisor.*"]
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
    TypeUrl    = "io.containerd.runsc.v1.options"
    ConfigPath = "/etc/containerd/runsc.toml"
```

## 3. runsc.toml

`/etc/containerd/runsc.toml`:

```toml
[runsc_config]
  platform = "systrap"     # or "kvm" where nested virtualization is available
  # ensure gVisor rootfs overlay is enabled — REQUIRED for checkpoint/restore
```

## 4. Snapshot store (CephFS) at a stable host path

Checkpoints are written on the pause node and read on the (possibly different)
resume node, so the store must be **RWX and node-visible** at the same path
everywhere. Use a shared CephFS PVC surfaced to the host at
`/var/lib/sandbox-snapshots`. The production way is the snapshotter DaemonSet
(it mounts the PVC with `mountPropagation: Bidirectional`); manual equivalent:

```sh
sudo mkdir -p /var/lib/sandbox-snapshots
# mount the shared CephFS export here (RWX), e.g. via ceph-fuse / kernel mount:
# sudo mount -t ceph <mon>:/snapshots /var/lib/sandbox-snapshots -o name=<user>,secret=<key>
```

`dev.gvisor.checkpoint.host-image-path` values point under this path
(`/var/lib/sandbox-snapshots/<sandbox-id>/<version>`), so any node can restore.

## 5. Restart containerd + verify

```sh
sudo systemctl restart containerd       # drain the node first if it serves traffic

# verify the patched shim is in place
sha256sum /usr/local/bin/containerd-shim-runsc-v1
grep -ac dev.gvisor.checkpoint.host-image-path /usr/local/bin/containerd-shim-runsc-v1   # expect 1
runsc --version
```

## 6. RuntimeClass (once per cluster)

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata: { name: gvisor }
handler: runsc
```

## 7. Production rollout — DaemonSet installer

Bake both binaries into an image and run a privileged DaemonSet on sandbox nodes
that performs steps 1–5 (install binaries, merge config, mount the CephFS store,
restart containerd) and then idles. Roll out node-by-node and **drain before
restarting containerd**. Target only sandbox nodes with a nodeSelector:

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
      nodeSelector: { sandbox.neevcloud.com/runsc: "true" }
      containers:
        - name: installer
          image: <registry>/gvisor-cr-installer:5a65ec1f-<arch>
          securityContext: { privileged: true }
          volumeMounts:
            - { name: hostbin, mountPath: /host/usr/local/bin }
            - { name: hostetc, mountPath: /host/etc/containerd }
            - { name: snapstore, mountPath: /var/lib/sandbox-snapshots, mountPropagation: Bidirectional }
          command: ["/bin/sh","-c","install -m0755 /artifacts/* /host/usr/local/bin/ && /artifacts/configure.sh && sleep infinity"]
      volumes:
        - { name: hostbin, hostPath: { path: /usr/local/bin } }
        - { name: hostetc, hostPath: { path: /etc/containerd } }
        - name: snapstore
          persistentVolumeClaim: { claimName: sandbox-snapshots }   # CephFS RWX PVC
```

## 8. Rollback

Keep the stock `runsc` + `containerd-shim-runsc-v1` in the node image. Rollback =
redeploy the previous installer/node image and restart containerd. The
annotation is inert on a stock shim (pods cold-start), so a partial rollout
degrades gracefully.

---

See [SETUP.md](SETUP.md) for the build/distribute overview and
[docs/walkthrough.md](docs/walkthrough.md) for how it all fits together.
