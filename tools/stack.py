#!/usr/bin/env python3
"""Combine upstream gVisor pull requests into one downstream release branch.

Reads a release manifest (releases/<tag>.yaml), cherry-picks the upstream
commits it selects onto the release tag it names, and records what came out in a
sibling lock file. Cherry-picks are deterministic -- fixed committer identity,
author date reused as the committer date -- so a dry run in a pull request
produces exactly the commit SHAs the real release will carry, and the lock file
can be reviewed in the manifest diff rather than written back by CI.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_FORK = "mayur-tolexo/gvisor"

# A fixed committer makes the cherry-picked SHAs reproducible. Without it every
# run stamps a new committer date and the lock file could never be pre-computed.
COMMITTER_NAME = "neev release bot"
COMMITTER_EMAIL = "release-bot@users.noreply.github.com"


class StackError(Exception):
    """A manifest or cherry-pick problem that needs an operator decision."""


class CommandError(StackError):
    """A subprocess failed; carries its stderr so the message is actionable."""

    def __init__(self, args: list[str], proc: subprocess.CompletedProcess):
        self.args_run = args
        self.proc = proc
        detail = (proc.stderr or proc.stdout or "").strip()
        super().__init__(f"{' '.join(args)} exited {proc.returncode}\n{detail}")


def run(args: list[str], *, env: dict | None = None, check: bool = True) -> str:
    """Run a command and return its stdout, stripped."""
    proc = subprocess.run(args, env=env, check=False, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise CommandError(args, proc)
    return proc.stdout.strip()


def git(repo: str, *args: str, **kw) -> str:
    """Run a git command inside `repo`."""
    return run(["git", "-C", repo, *args], **kw)


@dataclass
class Entry:
    """One upstream pull request and the commits taken from it."""

    pr: int
    source: str
    why: str
    pick: list[str]
    skip: list[str] = field(default_factory=list)
    # Set when an upstream commit does not apply to the base and a hand-adapted
    # equivalent on the fork stands in for it: {"ref": <fork branch>,
    # "map": {<upstream sha>: <fork sha>}}.
    rebased: dict | None = None


@dataclass
class Manifest:
    """The inputs of one release: a base tag and the stack applied to it."""

    tag: str
    base: str
    stack: list[Entry]
    frozen: bool = False
    path: Path | None = None

    @property
    def branch(self) -> str:
        """Fork branch carrying the stack, derived from the release tag."""
        # gvisor-cr-20260905 -> neev/cr-20260905; the tag already says "cr".
        return "neev/" + self.tag.removeprefix("gvisor-")

    @property
    def base_branch(self) -> str:
        """Pristine copy of the upstream tag that the review PR targets."""
        return f"neev/base-{self.base}"

    @property
    def lock_path(self) -> Path:
        """Sibling lock file recording what this manifest produced."""
        assert self.path is not None
        return self.path.parent / (self.path.stem + ".lock.yaml")


def parse_manifest(path: Path) -> Manifest:
    """Load and validate a release manifest."""
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise StackError(f"{path}: expected a YAML mapping")

    for key in ("tag", "base", "stack"):
        if not data.get(key):
            raise StackError(f"{path}: missing required key '{key}'")
    if not isinstance(data["stack"], list):
        raise StackError(f"{path}: 'stack' must be a list")

    entries = []
    for i, raw in enumerate(data["stack"]):
        where = f"{path}: stack[{i}]"
        for key in ("pr", "source", "why", "pick"):
            if not raw.get(key):
                raise StackError(f"{where}: missing required key '{key}'")
        pick = [str(s) for s in raw["pick"]]
        skip = [str(s) for s in raw.get("skip", [])]
        overlap = set(pick) & set(skip)
        if overlap:
            raise StackError(f"{where}: {sorted(overlap)} in both pick and skip")
        for name, shas in (("pick", pick), ("skip", skip)):
            if len(set(shas)) != len(shas):
                raise StackError(f"{where}: duplicate SHA in {name}")
        rebased = raw.get("rebased")
        if rebased is not None:
            if not isinstance(rebased, dict) or not rebased.get("ref") \
                    or not rebased.get("map"):
                raise StackError(f"{where}: rebased needs both 'ref' and 'map'")
            unknown = set(rebased["map"]) - set(pick)
            if unknown:
                raise StackError(
                    f"{where}: rebased maps {sorted(unknown)}, which is not in "
                    f"pick -- only a picked commit can be substituted")

        entries.append(Entry(pr=int(raw["pr"]), source=raw["source"],
                             why=raw["why"], pick=pick, skip=skip,
                             rebased=rebased))

    return Manifest(tag=data["tag"], base=data["base"], stack=entries,
                    frozen=bool(data.get("frozen", False)), path=path)


def fork_url(fork: str) -> str:
    """Git URL for a fork given as owner/name, or pass a URL or path through."""
    if "://" in fork or fork.startswith("/") or fork.startswith("git@"):
        return fork
    return f"https://github.com/{fork}.git"


def check_rebased_refs(manifest: Manifest, fork: str) -> list[str]:
    """Report every `rebased.ref` in this manifest that no longer exists on the fork.

    Runs even on a frozen manifest. A frozen release is never rebuilt, so a ref
    renamed out from under it goes unnoticed until someone does rebuild -- which is
    exactly when they can least afford a surprise.
    """
    url = fork_url(fork)
    missing = []
    for entry in manifest.stack:
        if not entry.rebased:
            continue
        ref = entry.rebased["ref"]
        if not run(["git", "ls-remote", url, f"refs/heads/{ref}"]):
            missing.append(f"PR {entry.pr}: rebased.ref {ref} does not exist on {fork}")
    return missing


def gh_pr_commits(source: str, pr: int) -> list[str]:
    """Return the commit SHAs on a pull request head, in the PR's own order.

    Asks GitHub rather than deriving a git range: a PR head is based on master,
    so `base..head` would sweep in hundreds of unrelated upstream commits.
    """
    out = run(["gh", "pr", "view", str(pr), "--repo", source,
               "--json", "commits"])
    return [c["oid"] for c in json.loads(out)["commits"]]


def fetch_pr_head(repo: str, entry: Entry) -> None:
    """Fetch a pull request head into a local ref so its commits are pickable."""
    url = f"https://github.com/{entry.source}.git"
    git(repo, "fetch", "--quiet", url,
        f"+refs/pull/{entry.pr}/head:refs/neev-stack/pr{entry.pr}")


def fetch_rebased_ref(repo: str, entry: Entry, fork: str) -> str:
    """Fetch the fork branch holding this entry's adapted commits.

    Returns the local ref name. The adapted commits must live on a pushed branch
    so a release stays rebuildable from remotes alone.
    """
    ref = f"refs/neev-stack/rebased-pr{entry.pr}"
    git(repo, "fetch", "--quiet", fork_url(fork),
        f"+refs/heads/{entry.rebased['ref']}:{ref}")
    return ref


def _substitute(repo: str, entry: Entry, upstream: str, ref: str) -> str:
    """Return the commit to apply for `upstream`, honouring any substitution."""
    for prefix, replacement in (entry.rebased or {}).get("map", {}).items():
        if not upstream.startswith(prefix):
            continue
        sha = git(repo, "rev-parse", "--verify", "--quiet",
                  f"{replacement}^{{commit}}", check=False)
        if not sha:
            raise StackError(
                f"PR {entry.pr}: rebased commit {replacement} not found on "
                f"{entry.rebased['ref']}")
        run(["git", "-C", repo, "merge-base", "--is-ancestor", sha, ref])
        return sha
    return upstream


def _resolve(prefix: str, actual: list[str], where: str) -> str:
    """Expand a manifest SHA prefix to the full SHA it names on the PR head."""
    hits = [a for a in actual if a.startswith(prefix)]
    if not hits:
        raise StackError(
            f"{where}: {prefix} is not a commit on this pull request head")
    if len(hits) > 1:
        raise StackError(f"{where}: {prefix} is ambiguous ({', '.join(hits)})")
    return hits[0]


def check_exhaustive(entry: Entry, actual: list[str]) -> dict[str, str]:
    """Require pick + skip to account for every commit on the PR head.

    This is the gate that turns an open pull request gaining a commit into a
    loud failure instead of a silent change in what gets shipped.
    """
    where = f"PR {entry.pr}"
    declared = {}
    for prefix in entry.pick:
        declared[_resolve(prefix, actual, where)] = prefix
    for prefix in entry.skip:
        declared[_resolve(prefix, actual, where)] = prefix

    unaccounted = [a for a in actual if a not in declared]
    if unaccounted:
        lines = "\n".join(f"    {a[:10]}" for a in unaccounted)
        raise StackError(
            f"{where}: these commits are on the pull request head but appear in "
            f"neither pick nor skip:\n{lines}\n"
            f"  The pull request moved. Re-pin it, or list the commit in skip "
            f"with a reason.")

    return {p: full for full, p in declared.items()}


@dataclass
class Picked:
    """One cherry-picked commit: where it came from and what it became."""

    pr: int
    upstream: str
    applied: str
    cherry: str


@dataclass
class Lock:
    """The outputs of a combine run, serialised next to the manifest."""

    tag: str
    base: str
    base_sha: str
    branch: str
    branch_sha: str
    version_stamp: str
    fork: str
    picked: list[Picked]

    def to_dict(self) -> dict:
        """Plain-data form written to <tag>.lock.yaml."""
        return {
            "tag": self.tag,
            "base": self.base,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "branch_sha": self.branch_sha,
            "version_stamp": self.version_stamp,
            "fork": self.fork,
            "picked": [{"pr": p.pr, "upstream": p.upstream,
                        "applied": p.applied, "cherry": p.cherry}
                       for p in self.picked],
        }


def _cherry_pick(repo: str, sha: str, pr: int) -> str:
    """Cherry-pick one commit deterministically and return the new SHA.

    Reuses the commit's own author date as the committer date so the resulting
    SHA depends only on the base, the commit, and its position in the stack.

    Deliberately not `-x`: that records "(cherry picked from commit ...)" in the
    message, putting an upstream commit into every commit on the fork. The
    upstream link lives in the lock file instead.
    """
    author_date = git(repo, "log", "-1", "--format=%aI", sha)
    env = {
        **os.environ,
        "GIT_COMMITTER_NAME": COMMITTER_NAME,
        "GIT_COMMITTER_EMAIL": COMMITTER_EMAIL,
        "GIT_COMMITTER_DATE": author_date,
    }
    try:
        run(["git", "-C", repo, "cherry-pick", sha], env=env)
    except CommandError:
        conflicts = git(repo, "diff", "--name-only", "--diff-filter=U",
                        check=False)
        git(repo, "cherry-pick", "--abort", check=False)
        listing = "\n".join(f"    {f}" for f in conflicts.splitlines()) or \
            "    (none reported)"
        raise StackError(
            f"PR {pr}: {sha[:10]} does not apply cleanly onto this base.\n"
            f"  Conflicting paths:\n{listing}\n"
            f"  Resolve it upstream or reorder the stack; this tool will not "
            f"invent a resolution.")
    return git(repo, "rev-parse", "HEAD")


def build_stack(manifest: Manifest, repo: str, fork: str, *,
                resolver=gh_pr_commits, fetcher=fetch_pr_head,
                rebase_fetcher=fetch_rebased_ref) -> Lock:
    """Cherry-pick the manifest's stack onto its base tag inside `repo`.

    Builds on a detached HEAD: a worktree shares its clone's ref namespace, so
    naming a branch here would leave one behind in the caller's checkout. The
    branch name is applied at push time, from the resulting SHA.
    """
    base_sha = git(repo, "rev-parse", f"{manifest.base}^{{commit}}")
    git(repo, "checkout", "--quiet", "--detach", base_sha)

    picked: list[Picked] = []
    for entry in manifest.stack:
        fetcher(repo, entry)
        actual = resolver(entry.source, entry.pr)
        if not actual:
            raise StackError(f"PR {entry.pr}: no commits reported on the head")
        chosen = check_exhaustive(entry, actual)
        wanted = {chosen[p] for p in entry.pick}
        # Exhaustiveness is always checked against the upstream pull request, so
        # a substituted entry still fails loudly when that pull request moves.
        rebased_ref = rebase_fetcher(repo, entry, fork) if entry.rebased else ""
        # Apply in the pull request's own order, not the manifest's, so a
        # manifest listing SHAs out of order still produces the intended stack.
        for sha in [a for a in actual if a in wanted]:
            applied = _substitute(repo, entry, sha, rebased_ref)
            picked.append(Picked(pr=entry.pr, upstream=sha, applied=applied,
                                 cherry=_cherry_pick(repo, applied, entry.pr)))

    return Lock(
        tag=manifest.tag,
        base=manifest.base,
        base_sha=base_sha,
        branch=manifest.branch,
        branch_sha=git(repo, "rev-parse", "HEAD"),
        version_stamp=git(repo, "describe", "--tags", "--long", "--abbrev=12"),
        fork=fork,
        picked=picked,
    )


def load_lock(path: Path) -> dict | None:
    """Read a committed lock file, or None when the release has no lock yet."""
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text())


def dump_lock(lock: Lock) -> str:
    """Serialise a lock in the stable key order the committed file uses."""
    return yaml.safe_dump(lock.to_dict(), sort_keys=False, default_flow_style=False)


def _describe_diff(existing: dict, fresh: dict) -> str:
    """List the fields where a committed lock and a rebuilt one disagree."""
    lines = []
    for key in sorted(set(existing) | set(fresh)):
        was, now = existing.get(key), fresh.get(key)
        if was != now:
            lines.append(f"  {key}:\n    committed: {was}\n    rebuilt:   {now}\n")
    return "".join(lines)


def reconcile_lock(manifest: Manifest, lock: Lock, *, write: bool,
                   allow_rewrite: bool) -> None:
    """Compare the freshly built stack against the committed lock file.

    The lock is the promise of what a tag contains. A published tag whose branch
    has moved is the drift this whole pipeline exists to prevent, so a mismatch
    stops the run unless it is explicitly authorised.
    """
    path = manifest.lock_path
    existing = load_lock(path)
    fresh = lock.to_dict()

    if existing == fresh:
        return

    if existing is None:
        if write:
            path.write_text(dump_lock(lock))
            print(f"wrote {path}", file=sys.stderr)
            return
        raise StackError(
            f"{path} does not exist.\n"
            f"  Re-run with --write-lock and commit the result:\n\n"
            f"{dump_lock(lock)}")

    if not allow_rewrite:
        raise StackError(
            f"{path} does not match the stack built from {manifest.path}.\n"
            f"{_describe_diff(existing, fresh)}"
            f"  Something the manifest points at moved. Re-pin it, or pass "
            f"--allow-rewrite if this tag was never published.")

    if write:
        path.write_text(dump_lock(lock))
        print(f"rewrote {path}", file=sys.stderr)


def pr_body(manifest: Manifest, lock: Lock) -> str:
    """Render the review PR body for the fork.

    Deliberately carries no upstream reference -- not a pull request number, not
    an upstream commit. Provenance lives in the release repository, in the lock
    file and the generated release notes.
    """
    lines = [
        f"Stack for `{manifest.tag}`, built on `{manifest.base}`.",
        "",
        f"Version stamp: `{lock.version_stamp}`",
        "",
    ]
    for entry in manifest.stack:
        lines.append(f"### {entry.why}")
        lines.append("")
        for p in [p for p in lock.picked if p.pr == entry.pr]:
            lines.append(f"- `{p.cherry[:10]}`")
        lines.append("")
    lines.append(
        "Generated from the release manifest. Edit the manifest, not this "
        "branch — the branch is rebuilt from it.")
    return "\n".join(lines)


def push_ref(repo: str, url: str, src: str, dst: str) -> None:
    """Force-push one ref, leased against the value observed a moment earlier.

    The lease needs an explicit expected value: pushing to a URL leaves no
    remote-tracking ref, and a bare --force-with-lease then rejects every push
    to a branch that already exists. Reading the value here means the lease only
    catches a push racing this one -- what actually protects a published release
    from being rewritten is the lock file and the frozen flag.
    """
    expected = run(["git", "-C", repo, "ls-remote", url, dst]).split("\t")[0]
    git(repo, "push", f"--force-with-lease={dst}:{expected}", url, f"{src}:{dst}")


def mirror_ref(manifest: Manifest, index: int) -> str:
    """Fork ref holding one entry's mirrored input.

    Numbered by position in the stack rather than named after an upstream pull
    request; the lock file records which commit each one holds.
    """
    return f"refs/heads/neev/mirror/{manifest.tag}/{index:02d}"


def mirror_target(entry: Entry, lock: Lock) -> str:
    """The upstream commit a pull request's mirror should point at.

    The last commit actually picked, never the pull request head: a head can end
    in commits the manifest rejects, and mirroring those preserves what we
    deliberately did not ship.
    """
    picked = [p.upstream for p in lock.picked if p.pr == entry.pr]
    if not picked:
        raise StackError(f"PR {entry.pr}: nothing picked, cannot mirror")
    return picked[-1]


def ensure_base_branch(repo: str, url: str, manifest: Manifest,
                       lock: Lock) -> None:
    """Create the pristine base branch, or verify it still is one.

    Never force-pushes. The branch exists only to give the review pull request a
    base that is stock upstream, so the diff is exactly the stack. If it has
    moved -- merging the review pull request into it does exactly that -- force
    it back would silently discard whatever moved it, so stop and say so.
    """
    ref = f"refs/heads/{manifest.base_branch}"
    current = run(["git", "-C", repo, "ls-remote", url, ref]).split("\t")[0]
    if current == lock.base_sha:
        return
    if not current:
        git(repo, "push", url, f"{lock.base_sha}:{ref}")
        return
    raise StackError(
        f"{manifest.base_branch} is at {current[:12]}, not the base tag "
        f"{manifest.base} ({lock.base_sha[:12]}).\n"
        f"  It must stay a pristine copy of the tag so the review pull request "
        f"shows only the stack.\n"
        f"  The review pull request is a review artifact -- close it, never "
        f"merge it. Reset or delete the branch and re-run.")


def push_and_review(manifest: Manifest, lock: Lock, repo: str,
                    fork: str) -> str | None:
    """Push the base copy, mirrors and stack branch, then open or update the PR.

    Returns the review PR URL. The mirrors exist because an upstream pull
    request can be closed and its fork deleted; a checkpoint taken under a build
    stays readable only by that exact build, so the inputs have to outlive the
    pull requests they came from.

    Each mirror points at the last commit actually picked, not the pull request
    head. A head can end in commits the manifest rejects -- a merge of upstream
    master, say -- and mirroring those both preserves what we deliberately did
    not ship and drags their workflow-file changes into the push, which GitHub
    rejects unless the token may write workflows.
    """
    url = fork_url(fork)

    ensure_base_branch(repo, url, manifest, lock)
    for index, entry in enumerate(manifest.stack, start=1):
        push_ref(repo, url, mirror_target(entry, lock),
                 mirror_ref(manifest, index))
    push_ref(repo, url, lock.branch_sha, f"refs/heads/{manifest.branch}")

    title = f"cr stack for {manifest.tag} on {manifest.base}"
    body = pr_body(manifest, lock)
    existing = run(["gh", "pr", "list", "--repo", fork, "--head",
                    manifest.branch, "--state", "open", "--json", "url",
                    "--jq", ".[0].url // empty"])
    if existing:
        run(["gh", "pr", "edit", existing, "--repo", fork, "--title", title,
             "--body", body])
        return existing
    # Opened as a draft: this pull request exists to be read, and merging it moves
    # the base branch off the tag and breaks the next run. A draft cannot be merged
    # without deliberately marking it ready, which is the point.
    return run(["gh", "pr", "create", "--repo", fork, "--base",
                manifest.base_branch, "--head", manifest.branch,
                "--title", title, "--body", body, "--draft"])


def prepare_checkout(gvisor: str | None, stack: tempfile.TemporaryDirectory
                     ) -> str:
    """Return a gVisor working tree to build the stack in.

    An existing clone is used through a throwaway detached worktree so the
    caller's own branches are never touched; without one, clone fresh.
    """
    if gvisor:
        work = Path(stack.name) / "wt"
        run(["git", "-C", gvisor, "fetch", "--quiet", "--tags", "origin"],
            check=False)
        run(["git", "-C", gvisor, "worktree", "add", "--quiet", "--detach",
             str(work), "HEAD"])
        return str(work)

    work = Path(stack.name) / "gvisor"
    run(["git", "clone", "--quiet", "--filter=blob:none",
         "https://github.com/google/gvisor.git", str(work)])
    return str(work)


def release_checkout(gvisor: str | None, work: str) -> None:
    """Drop the throwaway worktree so it does not accumulate in the clone."""
    if gvisor:
        run(["git", "-C", gvisor, "worktree", "remove", "--force", work],
            check=False)


def main(argv: list[str] | None = None) -> int:
    """Entry point: build one manifest's stack, verify its lock, optionally push."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--gvisor", default=os.environ.get("GVISOR_SRC"),
                    help="existing gVisor clone to work in (default: clone one)")
    ap.add_argument("--fork", default=DEFAULT_FORK,
                    help=f"fork to push the stack to (default: {DEFAULT_FORK})")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and verify only; push nothing")
    ap.add_argument("--write-lock", action="store_true",
                    help="write the lock file instead of only checking it")
    ap.add_argument("--allow-rewrite", action="store_true",
                    help="permit a lock mismatch (unpublished tags only)")
    ap.add_argument("--skip-frozen", action="store_true",
                    help="treat a frozen manifest as nothing to do, not an error")
    ap.add_argument("--check-refs", action="store_true",
                    help="only verify this manifest's rebased refs still exist")
    args = ap.parse_args(argv)

    try:
        manifest = parse_manifest(args.manifest)
        # Checked before the frozen gate: a frozen manifest is precisely the one
        # whose refs rot unnoticed.
        if args.check_refs:
            missing = check_rebased_refs(manifest, args.fork)
            for line in missing:
                print(f"error: {line}", file=sys.stderr)
            return 1 if missing else 0
        # A frozen manifest is a record of a published build. Checking a batch
        # of manifests should step over it; releasing one must still fail.
        if manifest.frozen and args.skip_frozen:
            print(f"{args.manifest}: frozen, nothing to build", file=sys.stderr)
            return 0
        if manifest.frozen and not args.allow_rewrite:
            raise StackError(
                f"{args.manifest} is frozen: {manifest.tag} is published and "
                f"builds already in use can only restore checkpoints written by "
                f"that exact binary. Refusing to rebuild it.")

        stack_dir = tempfile.TemporaryDirectory(prefix="neev-stack-")
        work = prepare_checkout(args.gvisor, stack_dir)
        try:
            lock = build_stack(manifest, work, args.fork)
            reconcile_lock(manifest, lock, write=args.write_lock,
                           allow_rewrite=args.allow_rewrite)
            url = None
            if not args.dry_run:
                url = push_and_review(manifest, lock, work, args.fork)
        finally:
            release_checkout(args.gvisor, work)
            stack_dir.cleanup()
    except StackError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    print(json.dumps({"tag": lock.tag, "branch": lock.branch,
                      "branch_sha": lock.branch_sha,
                      "version_stamp": lock.version_stamp,
                      "review_pr": url}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
