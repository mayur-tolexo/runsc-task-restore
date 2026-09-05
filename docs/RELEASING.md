# Cutting a release

A release is one file. `releases/<tag>.yaml` names an upstream gVisor release
tag and the commits taken from each upstream pull request; everything else —
the fork branch, the review PR, both binaries, the checksums, the notes — is
derived from it.

## Cut a new release

1. Copy the newest manifest to `releases/gvisor-cr-<date>.yaml`. Set `base:` to
   the upstream release tag you want and `supersedes:` to the previous release.
2. Re-pin every open pull request. `pick` and `skip` together must name every
   commit GitHub reports on the PR head, or the tool refuses to build.
3. Build it locally and write the lock file:

   ```
   python3 tools/stack.py releases/gvisor-cr-<date>.yaml \
     --gvisor ~/path/to/gvisor --dry-run --write-lock
   ```

   `--gvisor` reuses an existing clone through a throwaway worktree; without it
   the tool clones gVisor itself. Commit the manifest and the lock together.
4. Open the pull request. `stack-check` re-runs the same build and fails if the
   lock does not match, so the SHAs in the diff are the SHAs that will ship.
5. Merge. `release` combines the stack, pushes `neev/cr-<date>` to the fork,
   opens the review PR against a pristine copy of the base tag, builds both
   arches from the exact commit, and creates a **draft** release.
6. Verify the draft's own binaries on kind, then publish:

   ```
   gh release edit gvisor-cr-<date> --repo mayur-tolexo/runsc-task-restore --draft=false
   ```

7. **Freeze the manifest in the same breath.** Set `frozen: true` on it and
   commit. A manifest pins commits on open pull requests, and those move — the
   moment one is rebased, an unfrozen published manifest fails its own gate and
   takes every later pull request touching `releases/` down with it. Freezing
   also encodes the real rule: those binaries are in use, and a checkpoint is
   only readable by the build that wrote it.

## When a commit will not apply

Some changes cannot be cherry-picked at all. PR 13326 predates upstream's own
`CreateWithFSRestore` API and has never applied cleanly to any release tag; what
actually ships is a hand-adapted equivalent kept on the fork. Record that with
`rebased`:

```yaml
  - pr: 13326
    source: google/gvisor
    why: Kubernetes pod checkpoint/restore via annotations
    pick: [7a438259cb]
    rebased:
      ref: pr13326-pr14277-20260822
      map:
        7a438259cb: b2995c8005
```

The adapted commit must be on a branch pushed to the fork, so a release stays
rebuildable from remotes alone. Exhaustiveness is still checked against the
upstream pull request, so a substituted entry still fails when that PR moves.

## The fork's pull requests carry no upstream references

Nothing pushed to the fork references anything upstream:

| on the fork | how |
|---|---|
| review pull request | describes the stack by purpose and by branch SHAs only |
| commit messages | cherry-picks omit `-x`, so no "cherry picked from commit" trailer |
| mirror refs | `neev/mirror/<tag>/NN`, numbered by position, never by pull request |

Provenance lives here instead, in `releases/<tag>.lock.yaml` and the generated
release notes. The lock records, per commit, the upstream SHA, the commit
actually applied, and what it became on the branch — so the link is never lost,
it just is not carried on the fork.

## Rules the tool enforces

| Rule | Why |
|---|---|
| `pick` + `skip` cover every commit on the PR head | an open PR gaining a commit must fail, not silently change what ships |
| a committed lock must match the rebuilt stack | a tag's contents must not move after the fact |
| `frozen: true` refuses to rebuild | published builds must stay bit-identical: a checkpoint is only readable by the build that wrote it |
| both arches must build before anything is uploaded | a half-uploaded release used to drop an arch from `SHA256SUMS` |
| the release is created as a draft | a bad release is effectively permanent |

## Prerequisite

`GVISOR_FORK_TOKEN` — a fine-grained token with `contents:write` and
`pull_requests:write` on `mayur-tolexo/gvisor` only. `GITHUB_TOKEN` cannot write
to another repository.

## Never rebuild a published release

Every published build must stay installed for as long as any checkpoint taken
under it needs to restore. `gvisor-cr-20260817` and `gvisor-cr-20260822` are
recorded as frozen manifests for provenance only.
