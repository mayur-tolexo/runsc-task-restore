# Upstream: disk-backed, pod-shared emptyDir captured by checkpoint

Goal: land the `patches/0002` behavior in `google/gvisor` main so the capability
comes from upstream rather than a private fork (forward-compatible; no permanent
bespoke shim code).

## Issue (filed)

https://github.com/google/gvisor/issues/13595 — "containerd-shim: support a
disk-backed, pod-shared emptyDir that runsc checkpoint can capture". It carries the
full problem statement and the real reproduction logs (the on-disk
`.gvisor.filestore.*`, the shared-but-RAM emptyDir, the `no checkpointable
filesystems` failure, and the `repeated submounts` StartError).

## PR (deferred — not yet filed)

To submit after the issue gets traction. The change is `patches/0002` proposed
upstream rather than carried in the fork.

Title: shim: keep a pod-shared emptyDir a bind so it's a disk-backed overlay

`UpdateVolumeAnnotations` rewrites every emptyDir carrying a mount hint: an empty one
becomes a memory-backed tmpfs, a `force-shared`/non-empty one a `share=shared` gofer
bind. Neither yields a mount that is disk-backed, shared across the Pod's containers,
and captured by `runsc checkpoint` — though runsc supports that for a `bind` mount
carrying `type=tmpfs` + `share=pod` (a disk-backed SelfOverlay with one shared master
across containers).

This adds that case: when an emptyDir is annotated `share=pod`, keep its OCI mount a
`bind` and set the hint type to `tmpfs`, instead of rewriting the mount to tmpfs (RAM)
or forcing `share=shared` (gofer). Both the sandbox branch (set hint type, skip the
empty/force-shared rewrite) and the sub-container branch (keep the bind, no
`ChangeMountType`) are touched. Adds a `multi_container_test` case asserting the
on-disk filestore exists, the two containers share the mount, and it is captured by
checkpoint/fscheckpoint. Reproduction logs: see issue #13595.
