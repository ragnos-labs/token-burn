"""Create a disposable repository, remove its worktree, and restore the saved work.

The output directory must be new. All repositories, journals, leases and evidence
stay inside it. No remote or existing user repository is accessed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from token_burn.worktree.archive import create_archive, restore_archive
from token_burn.worktree.git import git
from token_burn.worktree.guards import remove_worktree
from token_burn.worktree.leases import LocalLeases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.output_dir.expanduser().absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ["TOKEN_BURN_STATE_DIR"] = str(root / "operations")
    os.environ["TOKEN_BURN_WORKTREE_STATE_DIR"] = str(root / "leases")
    os.environ["TOKEN_BURN_OTEL_ENABLED"] = "0"
    repo = root / "repository"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "note.txt").write_text("original\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "synthetic demo")
    target = root / "worktree"
    git(repo, "worktree", "add", "--quiet", "-b", "demo", str(target))
    leases = LocalLeases()
    owner = leases.claim(target, owner_id="demo-task")
    (target / "note.txt").write_text("staged\n")
    git(target, "add", "note.txt")
    (target / "note.txt").write_text("working\n")
    leases.release(target, lease_id=owner.lease_id)
    archive = root / "archive"
    create_archive(target, archive)
    removal = remove_worktree(target, archive)
    restored = root / "restored"
    recovery = restore_archive(archive, restored)
    result = {
        "demo_dir": str(root),
        "removed": removal["removed"],
        "restore_verified": recovery["restore_verified"],
        "staged_version": git(restored, "show", ":note.txt").decode(),
        "working_version": (restored / "note.txt").read_text(),
    }
    if result["staged_version"] != "staged\n" or result["working_version"] != "working\n":
        raise RuntimeError("demo recovery verification failed")
    (root / "demo-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
