#!/usr/bin/env python3
"""Tests for tools/stack.py.

Everything runs against a synthetic git repository built in a temp directory, so
the suite never touches the network, gVisor, or the fork. The GitHub lookup and
the PR fetch are injected as fakes.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

import stack


def _git(repo: str, *args: str) -> str:
    """Run git in the fixture repo, failing loudly."""
    return subprocess.run(["git", "-C", repo, *args], check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def _commit(repo: str, name: str, content: str, message: str) -> str:
    """Write a file and commit it, returning the new SHA."""
    (Path(repo) / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class Fixture:
    """A synthetic repo shaped like the real one: a release tag plus a PR branch."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="stack-test-")
        self.repo = str(Path(self.tmp.name) / "repo")
        Path(self.repo).mkdir()
        _git(self.repo, "init", "--quiet", "-b", "master")

        self.root = _commit(self.repo, "a.txt", "one\n", "root")
        self.base = _commit(self.repo, "b.txt", "base\n", "base work")
        _git(self.repo, "tag", "release-test.0")

        # The PR branch forks from the tag and adds three commits: two wanted,
        # one that stands in for the chore/merge noise the manifest must skip.
        _git(self.repo, "checkout", "--quiet", "-b", "pr", "release-test.0")
        self.c1 = _commit(self.repo, "c1.txt", "one\n", "first real change")
        self.noise = _commit(self.repo, "noise.txt", "x\n", "chore: noise")
        self.c2 = _commit(self.repo, "c2.txt", "two\n", "second real change")
        _git(self.repo, "checkout", "--quiet", "master")

    def resolver(self, source: str, pr: int) -> list[str]:
        """Stand in for the GitHub commit listing."""
        return [self.c1, self.noise, self.c2]

    @staticmethod
    def fetcher(repo: str, entry: stack.Entry) -> None:
        """The fixture's commits are already local; nothing to fetch."""

    def manifest(self, tmpdir: Path, **overrides) -> stack.Manifest:
        """Write a manifest naming the fixture's PR and parse it back."""
        data = {
            "tag": "gvisor-cr-test",
            "base": "release-test.0",
            "stack": [{
                "pr": 99,
                "source": "google/gvisor",
                "why": "fixture",
                "pick": [self.c1[:10], self.c2[:10]],
                "skip": [self.noise[:10]],
            }],
        }
        data.update(overrides)
        path = tmpdir / "gvisor-cr-test.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return stack.parse_manifest(path)

    def build(self, manifest: stack.Manifest) -> stack.Lock:
        """Build the manifest's stack in the fixture repo."""
        return stack.build_stack(manifest, self.repo, "owner/fork",
                                 resolver=self.resolver, fetcher=self.fetcher)

    def close(self) -> None:
        self.tmp.cleanup()


class ManifestTest(unittest.TestCase):
    """Manifest parsing rejects the shapes that would ship the wrong thing."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _write(self, data: dict) -> Path:
        path = self.dir / "m.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return path

    def test_missing_key(self) -> None:
        path = self._write({"tag": "t", "base": "b"})
        with self.assertRaisesRegex(stack.StackError, "missing required key 'stack'"):
            stack.parse_manifest(path)

    def test_entry_missing_key(self) -> None:
        path = self._write({"tag": "t", "base": "b",
                            "stack": [{"pr": 1, "source": "a/b", "why": "w"}]})
        with self.assertRaisesRegex(stack.StackError, "missing required key 'pick'"):
            stack.parse_manifest(path)

    def test_pick_skip_overlap(self) -> None:
        path = self._write({"tag": "t", "base": "b", "stack": [
            {"pr": 1, "source": "a/b", "why": "w", "pick": ["aaa"], "skip": ["aaa"]}]})
        with self.assertRaisesRegex(stack.StackError, "both pick and skip"):
            stack.parse_manifest(path)

    def test_duplicate_sha(self) -> None:
        path = self._write({"tag": "t", "base": "b", "stack": [
            {"pr": 1, "source": "a/b", "why": "w", "pick": ["aaa", "aaa"]}]})
        with self.assertRaisesRegex(stack.StackError, "duplicate SHA in pick"):
            stack.parse_manifest(path)

    def test_branch_names_derive_from_tag(self) -> None:
        path = self._write({"tag": "gvisor-cr-20260905", "base": "release-20260831.0",
                            "stack": [{"pr": 1, "source": "a/b", "why": "w",
                                       "pick": ["aaa"]}]})
        m = stack.parse_manifest(path)
        self.assertEqual(m.branch, "neev/cr-20260905")
        self.assertEqual(m.base_branch, "neev/base-release-20260831.0")
        self.assertEqual(m.lock_path.name, "m.lock.yaml")


class BuildTest(unittest.TestCase):
    """The combine step: selection, exhaustiveness, determinism, conflicts."""

    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_picks_selected_commits_and_drops_skipped(self) -> None:
        lock = self.fx.build(self.fx.manifest(self.dir))
        self.assertEqual([p.upstream for p in lock.picked],
                         [self.fx.c1, self.fx.c2])
        subjects = _git(self.fx.repo, "log", "--format=%s",
                        f"release-test.0..{lock.branch_sha}").splitlines()
        self.assertNotIn("chore: noise", subjects)
        self.assertEqual(sorted(subjects),
                         ["first real change", "second real change"])

    def test_cherry_picked_sha_differs_from_upstream(self) -> None:
        """The identity rule the manifest and lock exist to keep straight."""
        lock = self.fx.build(self.fx.manifest(self.dir))
        for p in lock.picked:
            self.assertNotEqual(p.upstream, p.cherry)

    def test_commit_messages_carry_no_upstream_commit(self) -> None:
        """The fork must not record upstream SHAs in its commit messages."""
        lock = self.fx.build(self.fx.manifest(self.dir))
        for p in lock.picked:
            body = _git(self.fx.repo, "log", "-1", "--format=%B", p.cherry)
            self.assertNotIn("cherry picked from", body)
            self.assertNotIn(p.upstream[:10], body)

    def test_deterministic_across_runs(self) -> None:
        """Two builds of one manifest must produce identical SHAs."""
        first = self.fx.build(self.fx.manifest(self.dir))
        second = self.fx.build(self.fx.manifest(self.dir))
        self.assertEqual(first.branch_sha, second.branch_sha)
        self.assertEqual([p.cherry for p in first.picked],
                         [p.cherry for p in second.picked])

    def test_unaccounted_commit_fails(self) -> None:
        """A pull request gaining a commit must stop the build, not change it."""
        m = self.fx.manifest(self.dir)
        m.stack[0].skip = []
        with self.assertRaises(stack.StackError) as cm:
            self.fx.build(m)
        self.assertIn("neither pick nor skip", str(cm.exception))
        self.assertIn(self.fx.noise[:10], str(cm.exception))

    def test_unknown_sha_fails(self) -> None:
        m = self.fx.manifest(self.dir)
        m.stack[0].pick = ["deadbeef01", self.fx.c2[:10]]
        with self.assertRaisesRegex(stack.StackError, "not a commit on this"):
            self.fx.build(m)

    def test_manifest_order_does_not_change_apply_order(self) -> None:
        m = self.fx.manifest(self.dir)
        m.stack[0].pick = [self.fx.c2[:10], self.fx.c1[:10]]
        lock = self.fx.build(m)
        self.assertEqual([p.upstream for p in lock.picked],
                         [self.fx.c1, self.fx.c2])

    def test_conflict_reports_paths_and_leaves_no_pick_in_progress(self) -> None:
        # A commit touching b.txt from before the tag's own change to it.
        _git(self.fx.repo, "checkout", "--quiet", "-b", "conflicting", self.fx.root)
        bad = _commit(self.fx.repo, "b.txt", "different\n", "conflicting change")
        _git(self.fx.repo, "checkout", "--quiet", "master")

        m = self.fx.manifest(self.dir)
        m.stack[0].pick = [bad[:10]]
        m.stack[0].skip = []
        with self.assertRaises(stack.StackError) as cm:
            stack.build_stack(m, self.fx.repo, "owner/fork",
                              resolver=lambda s, p: [bad],
                              fetcher=self.fx.fetcher)
        self.assertIn("b.txt", str(cm.exception))
        self.assertFalse((Path(self.fx.repo) / ".git" / "CHERRY_PICK_HEAD").exists())


class LockTest(unittest.TestCase):
    """The lock file is the promise of what a tag contains."""

    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.manifest = self.fx.manifest(self.dir)
        self.lock = self.fx.build(self.manifest)

    def test_missing_lock_without_write_fails_and_shows_content(self) -> None:
        with self.assertRaises(stack.StackError) as cm:
            stack.reconcile_lock(self.manifest, self.lock, write=False,
                                 allow_rewrite=False)
        self.assertIn("--write-lock", str(cm.exception))
        self.assertIn(self.lock.branch_sha, str(cm.exception))

    def test_write_then_check_round_trips(self) -> None:
        stack.reconcile_lock(self.manifest, self.lock, write=True,
                             allow_rewrite=False)
        self.assertTrue(self.manifest.lock_path.exists())
        stack.reconcile_lock(self.manifest, self.lock, write=False,
                             allow_rewrite=False)

    def test_mismatch_is_refused(self) -> None:
        """The drift guard: a tag's recorded branch must not quietly move."""
        stack.reconcile_lock(self.manifest, self.lock, write=True,
                             allow_rewrite=False)
        moved = yaml.safe_load(self.manifest.lock_path.read_text())
        moved["branch_sha"] = "0" * 40
        self.manifest.lock_path.write_text(yaml.safe_dump(moved, sort_keys=False))
        with self.assertRaisesRegex(stack.StackError, "does not match the stack"):
            stack.reconcile_lock(self.manifest, self.lock, write=False,
                                 allow_rewrite=False)

    def test_allow_rewrite_overrides_mismatch(self) -> None:
        stack.reconcile_lock(self.manifest, self.lock, write=True,
                             allow_rewrite=False)
        moved = yaml.safe_load(self.manifest.lock_path.read_text())
        moved["branch_sha"] = "0" * 40
        self.manifest.lock_path.write_text(yaml.safe_dump(moved, sort_keys=False))
        stack.reconcile_lock(self.manifest, self.lock, write=True,
                             allow_rewrite=True)
        self.assertEqual(
            yaml.safe_load(self.manifest.lock_path.read_text())["branch_sha"],
            self.lock.branch_sha)


class FrozenTest(unittest.TestCase):
    """A published release refuses to rebuild: its binaries must stay bit-identical."""

    def test_frozen_manifest_refuses(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "frozen.yaml"
        path.write_text(yaml.safe_dump({
            "tag": "gvisor-cr-20260822", "base": "release-20260817.0",
            "frozen": True,
            "stack": [{"pr": 1, "source": "a/b", "why": "w", "pick": ["aaa"]}],
        }, sort_keys=False))
        self.assertEqual(stack.main([str(path), "--dry-run"]), 1)


class ForkPrBodyTest(unittest.TestCase):
    """The fork's pull requests must not reference anything upstream."""

    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.manifest = self.fx.manifest(Path(tmp.name))
        self.lock = self.fx.build(self.manifest)
        self.body = stack.pr_body(self.manifest, self.lock)

    def test_describes_the_stack(self) -> None:
        self.assertIn(self.manifest.tag, self.body)
        self.assertIn(self.lock.version_stamp, self.body)
        for p in self.lock.picked:
            self.assertIn(f"`{p.cherry[:10]}`", self.body)

    def test_carries_no_upstream_pull_request_number(self) -> None:
        for entry in self.manifest.stack:
            self.assertNotIn(str(entry.pr), self.body)
            self.assertNotIn(entry.source, self.body)

    def test_carries_no_upstream_commit(self) -> None:
        for p in self.lock.picked:
            self.assertNotIn(p.upstream[:10], self.body)
            self.assertNotIn(p.applied[:10], self.body)

    def test_carries_no_skipped_commit(self) -> None:
        self.assertNotIn(self.fx.noise[:10], self.body)


if __name__ == "__main__":
    unittest.main()


class RebasedTest(unittest.TestCase):
    """A commit that cannot apply to the base is substituted by a fork commit.

    PR 13326 has never cherry-picked cleanly onto any release tag; what ships is
    a hand-adapted equivalent. The manifest records both so provenance survives.
    """

    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        # An adapted stand-in for c1, living on its own branch off the base.
        _git(self.fx.repo, "checkout", "--quiet", "-b", "adapted", "release-test.0")
        self.adapted = _commit(self.fx.repo, "c1.txt", "adapted\n",
                               "adapted first real change")
        _git(self.fx.repo, "checkout", "--quiet", "master")

    def _rebase_fetcher(self, repo: str, entry: stack.Entry, fork: str) -> str:
        """The fixture's adapted branch is already local."""
        return "adapted"

    def _manifest(self) -> stack.Manifest:
        return self.fx.manifest(self.dir, stack=[{
            "pr": 99, "source": "google/gvisor", "why": "fixture",
            "pick": [self.fx.c1[:10], self.fx.c2[:10]],
            "skip": [self.fx.noise[:10]],
            "rebased": {"ref": "adapted",
                        "map": {self.fx.c1[:10]: self.adapted[:10]}},
        }])

    def _build(self, manifest: stack.Manifest) -> stack.Lock:
        return stack.build_stack(manifest, self.fx.repo, "owner/fork",
                                 resolver=self.fx.resolver,
                                 fetcher=self.fx.fetcher,
                                 rebase_fetcher=self._rebase_fetcher)

    def test_substituted_commit_is_applied(self) -> None:
        lock = self._build(self._manifest())
        first = lock.picked[0]
        self.assertEqual(first.upstream, self.fx.c1)
        self.assertEqual(first.applied, self.adapted)
        content = _git(self.fx.repo, "show", f"{first.cherry}:c1.txt")
        self.assertEqual(content, "adapted")

    def test_unsubstituted_commit_is_untouched(self) -> None:
        lock = self._build(self._manifest())
        second = lock.picked[1]
        self.assertEqual(second.applied, second.upstream)

    def test_exhaustiveness_still_checked_against_upstream(self) -> None:
        """Substitution must not disable the pull-request-moved guard."""
        m = self._manifest()
        m.stack[0].skip = []
        with self.assertRaisesRegex(stack.StackError, "neither pick nor skip"):
            self._build(m)

    def test_map_must_reference_a_picked_commit(self) -> None:
        with self.assertRaisesRegex(stack.StackError, "which is not in pick"):
            self.fx.manifest(self.dir, stack=[{
                "pr": 99, "source": "google/gvisor", "why": "fixture",
                "pick": [self.fx.c1[:10]], "skip": [self.fx.noise[:10],
                                                    self.fx.c2[:10]],
                "rebased": {"ref": "adapted",
                            "map": {self.fx.c2[:10]: self.adapted[:10]}},
            }])

    def test_map_without_ref_is_rejected(self) -> None:
        with self.assertRaisesRegex(stack.StackError, "both 'ref' and 'map'"):
            self.fx.manifest(self.dir, stack=[{
                "pr": 99, "source": "google/gvisor", "why": "fixture",
                "pick": [self.fx.c1[:10]],
                "skip": [self.fx.noise[:10], self.fx.c2[:10]],
                "rebased": {"map": {self.fx.c1[:10]: self.adapted[:10]}},
            }])

    def test_missing_rebased_commit_fails(self) -> None:
        m = self._manifest()
        m.stack[0].rebased = {"ref": "adapted", "map": {self.fx.c1[:10]: "cafe0123"}}
        with self.assertRaisesRegex(stack.StackError, "not found on"):
            self._build(m)

    def test_rebased_commit_must_be_on_the_named_ref(self) -> None:
        """A stray local commit must not sneak in as an adaptation."""
        m = self._manifest()
        m.stack[0].rebased = {"ref": "adapted",
                              "map": {self.fx.c1[:10]: self.fx.c2[:10]}}
        with self.assertRaises(stack.CommandError):
            self._build(m)


class PushTest(unittest.TestCase):
    """Pushing to a URL has no remote-tracking ref, so the lease needs a value."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="push-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.remote = str(root / "remote.git")
        subprocess.run(["git", "init", "--quiet", "--bare", self.remote],
                       check=True)
        self.repo = str(root / "work")
        Path(self.repo).mkdir()
        _git(self.repo, "init", "--quiet", "-b", "master")
        self.first = _commit(self.repo, "a.txt", "one\n", "one")

    def _remote_sha(self, ref: str = "refs/heads/probe") -> str:
        out = subprocess.run(["git", "ls-remote", self.remote, ref],
                             check=True, text=True,
                             stdout=subprocess.PIPE).stdout
        return out.split("\t")[0] if out else ""

    def test_creates_a_new_ref(self) -> None:
        stack.push_ref(self.repo, self.remote, "master", "refs/heads/probe")
        self.assertEqual(self._remote_sha(), self.first)

    def test_updates_an_existing_ref(self) -> None:
        """The case a bare --force-with-lease rejects outright."""
        stack.push_ref(self.repo, self.remote, "master", "refs/heads/probe")
        second = _commit(self.repo, "a.txt", "two\n", "two")
        stack.push_ref(self.repo, self.remote, "master", "refs/heads/probe")
        self.assertEqual(self._remote_sha(), second)

    def test_rewinds_an_existing_ref(self) -> None:
        """A rebuilt stack replaces the branch rather than fast-forwarding it."""
        _commit(self.repo, "a.txt", "two\n", "two")
        stack.push_ref(self.repo, self.remote, "master", "refs/heads/probe")
        _git(self.repo, "reset", "--hard", "--quiet", self.first)
        stack.push_ref(self.repo, self.remote, "master", "refs/heads/probe")
        self.assertEqual(self._remote_sha(), self.first)


class SkipFrozenTest(unittest.TestCase):
    """Checking a batch of manifests steps over frozen ones without failing."""

    def _frozen_manifest(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "frozen.yaml"
        path.write_text(yaml.safe_dump({
            "tag": "gvisor-cr-20260822", "base": "release-20260817.0",
            "frozen": True,
            "stack": [{"pr": 1, "source": "a/b", "why": "w", "pick": ["aaa"]}],
        }, sort_keys=False))
        return path

    def test_skip_frozen_succeeds(self) -> None:
        self.assertEqual(
            stack.main([str(self._frozen_manifest()), "--dry-run", "--skip-frozen"]), 0)

    def test_releasing_a_frozen_manifest_still_fails(self) -> None:
        self.assertEqual(stack.main([str(self._frozen_manifest()), "--dry-run"]), 1)


class VersionStampTest(unittest.TestCase):
    """The stamp must not depend on how large the clone happens to be."""

    def test_abbreviation_is_pinned(self) -> None:
        # git's default abbreviation grows with a repo's object count, so an
        # unpinned describe stamps a fresh clone and a full one differently.
        fx = Fixture()
        self.addCleanup(fx.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lock = fx.build(fx.manifest(Path(tmp.name)))
        suffix = lock.version_stamp.rsplit("-g", 1)[1]
        self.assertEqual(len(suffix), 12, lock.version_stamp)


class MirrorTargetTest(unittest.TestCase):
    """A mirror must not preserve commits the manifest rejected."""

    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manifest = self.fx.manifest(Path(self.tmp.name))
        self.lock = self.fx.build(self.manifest)

    def test_mirrors_the_last_picked_commit(self) -> None:
        self.assertEqual(stack.mirror_target(self.manifest.stack[0], self.lock),
                         self.fx.c2)

    def test_does_not_mirror_a_trailing_rejected_commit(self) -> None:
        """PR 14428 ends in a merge of master; mirroring it broke the push."""
        m = self.fx.manifest(Path(self.tmp.name), stack=[{
            "pr": 99, "source": "google/gvisor", "why": "fixture",
            "pick": [self.fx.c1[:10]],
            "skip": [self.fx.noise[:10], self.fx.c2[:10]],
        }])
        lock = self.fx.build(m)
        target = stack.mirror_target(m.stack[0], lock)
        self.assertEqual(target, self.fx.c1)
        self.assertNotEqual(target, self.fx.c2)

    def test_mirrors_upstream_not_the_adapted_commit(self) -> None:
        """The fragile input is the upstream commit; the adaptation is ours."""
        for p in self.lock.picked:
            self.assertIn(stack.mirror_target(self.manifest.stack[0], self.lock),
                          [q.upstream for q in self.lock.picked])


class MirrorRefTest(unittest.TestCase):
    """Mirror refs must not be named after an upstream pull request."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "gvisor-cr-20260831.yaml"
        path.write_text(yaml.safe_dump({
            "tag": "gvisor-cr-20260831", "base": "release-20260831.0",
            "stack": [
                {"pr": 13326, "source": "google/gvisor", "why": "w", "pick": ["aaa"]},
                {"pr": 14428, "source": "google/gvisor", "why": "w", "pick": ["bbb"]},
            ],
        }, sort_keys=False))
        self.manifest = stack.parse_manifest(path)

    def test_ref_is_numbered_not_pr_named(self) -> None:
        first = stack.mirror_ref(self.manifest, 1)
        second = stack.mirror_ref(self.manifest, 2)
        self.assertEqual(first, "refs/heads/neev/mirror/gvisor-cr-20260831/01")
        self.assertEqual(second, "refs/heads/neev/mirror/gvisor-cr-20260831/02")

    def test_ref_carries_no_pull_request_number(self) -> None:
        for i, entry in enumerate(self.manifest.stack, start=1):
            self.assertNotIn(str(entry.pr), stack.mirror_ref(self.manifest, i))
