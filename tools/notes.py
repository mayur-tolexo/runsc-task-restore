#!/usr/bin/env python3
"""Render release notes for one release from its manifest and lock file.

The scaffolding -- what is in the build, the version stamp, and the standing
warning that a release never replaces its predecessor -- is generated, because
it is identical every time and hand-writing it has already produced a wrong
cross-reference. Anything specific to the release goes in the manifest's own
`notes` block and is passed through verbatim.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

import stack

# Every published build must stay installed for as long as any checkpoint taken
# under it needs to restore, so this warning belongs on every release.
IMMUTABILITY = """\
A gVisor checkpoint can only be read by the exact build that wrote it. This
release does not replace {predecessor} -- that build stays published and must
remain installed for as long as any checkpoint taken under it needs to restore."""


def render(manifest: stack.Manifest, lock: dict) -> str:
    """Build the release body from the manifest's inputs and the lock's outputs."""
    raw = yaml.safe_load(manifest.path.read_text())

    out = [
        f"runsc + containerd-shim-runsc-v1 built from `{manifest.base}` with:",
        "",
    ]
    for entry in manifest.stack:
        out.append(f"- PR {entry.pr} — {entry.why}")
    out += [
        "",
        f"Version stamp: `{lock['version_stamp']}` (both arches).",
        "",
    ]

    predecessor = raw.get("supersedes")
    if predecessor:
        out += [IMMUTABILITY.format(predecessor=f"`{predecessor}`"), ""]

    if raw.get("notes"):
        out += [raw["notes"].rstrip(), ""]

    out += [
        "Provenance",
        "",
        "| upstream | in this build |",
        "| --- | --- |",
    ]
    for p in lock["picked"]:
        # An adapted commit is the interesting case: what shipped is not the
        # upstream commit, and the table is where that has to be visible.
        origin = f"{p['pr']} `{p['upstream'][:10]}`"
        if p["applied"] != p["upstream"]:
            origin += f" adapted as `{p['applied'][:10]}`"
        out.append(f"| {origin} | `{p['cherry'][:10]}` |")
    out += [
        "",
        f"Built from `{lock['fork']}` `{lock['branch']}` at `{lock['branch_sha']}`.",
    ]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Entry point: print the release body for the given manifest."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", type=Path)
    args = ap.parse_args(argv)

    manifest = stack.parse_manifest(args.manifest)
    lock = stack.load_lock(manifest.lock_path)
    if lock is None:
        print(f"error: {manifest.lock_path} does not exist", file=sys.stderr)
        return 1
    sys.stdout.write(render(manifest, lock))
    return 0


if __name__ == "__main__":
    sys.exit(main())
