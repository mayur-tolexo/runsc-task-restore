# Cutting a release

A release is one file. `releases/<tag>.yaml` names an upstream gVisor release tag
and, per upstream pull request, exactly which commits to take. Everything else —
the fork branch, the review pull request, both binaries, the checksums, the
release notes — is derived from it.

Nothing here needs prior knowledge of how a previous release was built. If the
manifest is right, the pipeline produces the same bytes every time.

## Before you start

| You need | Why |
|---|---|
| Python 3 with `pyyaml` | `tools/stack.py` |
| `gh`, authenticated | reads pull request commits, opens the review pull request |
| A gVisor clone (optional) | `--gvisor` makes local runs fast; without it the tool clones one |
| `GVISOR_FORK_TOKEN` in repo secrets | CI pushes to the fork; `GITHUB_TOKEN` cannot write cross-repo. Fine-grained, `contents:write` + `pull_requests:write` on the fork only |

The fork's ruleset must not block the pipeline's own refs. It force-updates
`neev/cr-*` and `neev/mirror/*` whenever a stack changes, so exclude `neev/**`
from any `non_fast_forward` or `deletion` rule, or give the release identity a
bypass. It never force-pushes `neev/base-*`.

## Add a pull request to the next release

1. Copy the newest manifest to `releases/gvisor-cr-<date>.yaml`. Set `base:` to
   the upstream release tag you want, and `supersedes:` to the release this one
   follows.

2. Add an entry for the pull request:

   ```yaml
     - pr: 14428
       source: google/gvisor
       why: bound the io drain in shim delete
       pick: [c04c9cbc2f]
       skip:
         - 91b6d56014  # merge of master into the branch, not a change
   ```

   `pick` and `skip` together must name **every** commit GitHub reports on that
   pull request's head. The tool refuses to build otherwise. That is deliberate:
   an open pull request gaining a commit becomes a loud failure instead of a
   silent change in what ships.

3. Re-pin every other open pull request in the manifest. Their commits move
   whenever the author rebases.

4. Build it and write the lock:

   ```
   python3 tools/stack.py releases/gvisor-cr-<date>.yaml \
     --gvisor ~/path/to/gvisor --dry-run --write-lock
   ```

   Commit the manifest and the lock together. The lock is the promise of what
   the tag contains; because cherry-picks are deterministic, the SHAs in the
   diff are the SHAs that will ship.

5. Open the pull request. `stack-check` re-runs the same build and fails if the
   lock does not match.

6. Merge it. `release` combines the stack, pushes the fork branches, opens the
   review pull request, builds both arches from the exact commit, and creates a
   **draft** release. Roughly 15 minutes.

7. Verify the draft (below), then publish and freeze.

### The version string is the checkpoint compatibility key

This is the single most load-bearing thing in the pipeline, and it is not
obvious from the code.

gVisor stamps `git describe` output into `runsc/version.version` at build time
(`runsc/BUILD`, via `x_defs` from `{STABLE_VERSION}`). `runsc/boot/restore.go`
writes that string into every checkpoint, and `runsc/boot/controller.go` compares
it on every restore:

```go
checkpointVersion := cm.restorer.metadata[VersionKey]
currentVersion := version.Version()
if checkpointVersion != currentVersion {
    return fmt.Errorf("runsc version does not match across checkpoint restore, ...")
}
```

A mismatch fails the restore outright. Three consequences:

- **Never recut a published release to correct its stamp.** The recut binary
  would be byte-different in that string alone, and every checkpoint taken under
  the original would stop restoring. A cosmetically wrong stamp is strictly
  better than a broken one — it still names the right commit, and save and
  restore agree because both come from the same binary.
- **Anything that changes `git describe` output changes checkpoint
  compatibility.** The pinned `--abbrev=12` and the upstream-tag fetch in the
  build job both feed it. Treat either as a compatibility-affecting change, not
  a formatting one.
- **Freezing on publish pins this string, not just provenance.** That is the real
  reason a published manifest must never rebuild.

`gvisor-cr-20260905` and `gvisor-cr-20260831` were built before the build job
fetched upstream tags, so their binaries report an older base
(`release-20260817.0-201-g...`) than their notes. They name the correct commit
and restore correctly. Leave them.

### A note on the version stamp

`runsc --version` on a node must match the `Version stamp` in the release notes.
gVisor derives its own stamp with `git describe`, so the build clone needs
upstream's release tags — the fork alone does not carry them. The build job
fetches them; if a stamp ever reports an older base than the notes, that fetch is
what went missing.

## Manifest reference

| Field | Meaning |
|---|---|
| `tag` | release tag, e.g. `gvisor-cr-20260831` |
| `base` | upstream release tag the stack sits on |
| `supersedes` | previous release; drives the "does not replace" note |
| `frozen` | set once published — refuses to rebuild |
| `notes` | free prose appended to the generated release notes |
| `stack[].pr` | upstream pull request number |
| `stack[].source` | repo holding it, e.g. `google/gvisor` |
| `stack[].why` | one line; becomes a release-note bullet and a review heading |
| `stack[].pick` | commits to take, upstream SHAs |
| `stack[].skip` | commits deliberately not taken — always comment the reason |
| `stack[].rebased` | substitute a hand-adapted commit; see below |

## When a commit will not apply

Some changes cannot be cherry-picked onto a newer base at all — upstream moved
the code they touch. Adapt the change by hand, push it to the fork, and point
the manifest at it:

1. Build the stack up to the failing commit in a worktree off the base tag,
   cherry-pick it, and resolve the conflict.
2. Push the resulting branch to the fork as `neev/adapted/<base tag>`.
3. Reference the adapted commit:

   ```yaml
       rebased:
         ref: neev/adapted/release-20260831.0
         map:
           c04c9cbc2f: 1562639f91   # upstream commit: adapted commit
   ```

The upstream commit stays pinned, so exhaustiveness is still checked against the
upstream pull request and a moved pull request still fails. The lock records
`upstream`, `applied` and `cherry` for every commit, so the chain is never lost.

Resolve conflicts by preserving both sides unless you have a reason not to.
Upstream often fixes the same bug differently; taking one side wholesale can
silently revert their fix.

## The fork

| Ref | Holds |
|---|---|
| `neev/base-<tag>` | pristine copy of the upstream tag — the review pull request's base |
| `neev/cr-<date>` | the stack |
| `neev/mirror/<tag>/NN` | each entry's last picked commit, so inputs outlive the pull requests |
| `neev/adapted/<tag>` | hand-adapted commits referenced by `rebased` |

**Never merge the review pull request.** It is a review artifact: its diff is the
stack over stock upstream. Merging it moves `neev/base-<tag>` off the tag, and
the next run stops rather than rewind it. Close it instead.

It is opened as a **draft** for exactly this reason — a draft cannot be merged
without being marked ready first. If you find yourself about to click that,
stop: nothing about the release needs it. When one has been merged anyway,
delete `neev/base-<tag>` and re-run; the tool recreates it from the tag.

Nothing pushed to the fork references anything upstream — not the pull request
body, not commit messages (cherry-picks omit `-x`), not ref names. Provenance
lives here, in the lock file and the release notes.

## Verify a draft before publishing

Install the draft's own binaries on a kind node, then:

| Check | Expect |
|---|---|
| `kubectl exec <pod> -- dmesg \| head -1` | `Starting gVisor...` |
| `runsc update --cpu-quota=800000 <sandbox>` | `nproc` grows, 0 restarts |
| in-place pod resize | `nproc` tracks the new limit, 0 restarts |
| create/delete several pods | shim process count returns to baseline |
| `ctr -n k8s.io tasks checkpoint --image-path ...` | `checkpoint.img`, `pages.img` written |
| restore pod with `dev.gvisor.internal.restore.host-image-path` | carries the source's in-memory state |

Write a marker into the source pod before checkpointing and read it back in the
restored pod. That is the only check that proves restore rather than a cold boot.

## Publish, then freeze

```
gh release edit gvisor-cr-<date> --repo mayur-tolexo/runsc-task-restore --draft=false
```

Then set `frozen: true` on the manifest and commit it, in the same sitting. A
manifest pins commits on open pull requests; the moment one is rebased, an
unfrozen published manifest fails its own gate and takes every later pull
request touching `releases/` with it. Freezing also encodes the real rule: those
binaries are in use, and a checkpoint is only readable by the build that wrote
it.

## What the pipeline enforces

| Rule | Why |
|---|---|
| `pick` + `skip` cover every commit on the head | a moved pull request must fail, not silently change what ships |
| the lock must match the rebuilt stack | a tag's contents must not move after the fact |
| `frozen` refuses to rebuild | published builds stay bit-identical — and their version string is the key their checkpoints restore against |
| both arches build before anything uploads | a half-uploaded release once dropped an arch from `SHA256SUMS` |
| the release is a draft | a bad release is effectively permanent |
| the base branch is never force-pushed | force would discard whatever moved it |

## When it fails

| Message | Cause | Fix |
|---|---|---|
| `X is not a commit on this pull request head` | the pull request was rebased | re-pin `pick`/`skip` from `gh pr view N --json commits` |
| `neither pick nor skip` | the pull request gained a commit | pick it, or skip it with a reason |
| `does not apply cleanly onto this base` | upstream moved the code | adapt by hand, use `rebased` |
| `lock does not match` | inputs changed since the lock was written | rebuild with `--write-lock`; if the tag is published, freeze it instead |
| `is frozen` | rebuilding a published release | intended — cut a new tag |
| `base-<tag> is at X, not the base tag` | the review pull request was merged into it | reset or delete that branch; never merge the review pull request |
| `Cannot force-push to this branch` | fork ruleset covers `neev/**` | exclude `neev/**`, or add a bypass |
| `without \`workflow\` scope` | a mirrored commit changes a workflow file | mirror the last *picked* commit, not the head — the tool already does |
| `runsc version does not match across checkpoint restore` | restoring under a different build than wrote the checkpoint | install the build named in that release; never recut a published release to change its stamp |
