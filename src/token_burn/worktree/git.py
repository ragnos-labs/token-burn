# SPDX-License-Identifier: Apache-2.0
"""Local Git calls and exact linked-worktree identity."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


class WorktreeError(RuntimeError):
    """A stable reason code; raw Git output stays out of telemetry and CLI errors."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def git(repo: Path, *args: str, input: bytes | None = None, env: dict | None = None) -> bytes:
    # Git's ambient directory/index overrides must not change an explicit target.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="file",
        GIT_AUTHOR_NAME="token-burn recovery",
        GIT_AUTHOR_EMAIL="recovery@token-burn.invalid",
        GIT_COMMITTER_NAME="token-burn recovery",
        GIT_COMMITTER_EMAIL="recovery@token-burn.invalid",
    )
    if env:
        environment.update(env)
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "gc.auto=0",
                "-c",
                "maintenance.auto=false",
                "-C",
                str(repo),
                *args,
            ],
            input=input,
            capture_output=True,
            env=environment,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError("git_unavailable") from exc
    if result.returncode:
        raise WorktreeError("git_operation_failed")
    return result.stdout


def git_path(repo: Path, name: str) -> Path:
    value = git(repo, "rev-parse", "--path-format=absolute", name)
    return Path(os.fsdecode(value.removesuffix(b"\n"))).resolve()


def common_dir(repo: Path) -> Path:
    return git_path(repo, "--git-common-dir")


def registered_worktrees(repo: Path) -> dict[Path, dict[str, str]]:
    entries: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for field in git(repo, "worktree", "list", "--porcelain", "-z").split(b"\0"):
        if not field:
            if "worktree" in current:
                entries[Path(current["worktree"]).resolve()] = current
            current = {}
            continue
        key, _, value = field.partition(b" ")
        current[key.decode("ascii")] = os.fsdecode(value)
    if "worktree" in current:
        entries[Path(current["worktree"]).resolve()] = current
    return entries


def resolve_worktree(worktree: Path | str) -> Path:
    supplied = Path(worktree).expanduser().absolute()
    if supplied.is_symlink():
        raise WorktreeError("unsafe_target")
    return supplied.resolve(strict=True)


def identity(worktree: Path | str, *, require_unlocked: bool = True) -> dict[str, Any]:
    target = resolve_worktree(worktree)
    common = common_dir(target)
    git_dir = git_path(target, "--absolute-git-dir")
    if common == git_dir or not (target / ".git").is_file():
        raise WorktreeError("primary_checkout_protected")
    rows = registered_worktrees(target)
    record = rows.get(target)
    if record is None or "bare" in record:
        raise WorktreeError("unregistered_worktree")
    if require_unlocked and "locked" in record:
        raise WorktreeError("git_worktree_locked")
    if "prunable" in record:
        raise WorktreeError("worktree_state_unknown")
    for setting in git(target, "config", "--list", "--null").split(b"\0"):
        key, _, value = setting.partition(b"\n")
        key = key.lower()
        if (
            key.startswith(b"filter.")
            and key.endswith((b".clean", b".smudge", b".process"))
            and value
        ):
            raise WorktreeError("configured_filter_requires_adapter")
        if key == b"extensions.partialclone" or (
            (key.endswith(b".promisor") or key == b"core.sparsecheckout")
            and value.lower() not in {b"false", b"0", b"no", b"off"}
        ):
            raise WorktreeError("incomplete_object_store")
    # Submodules, sparse indexes and incomplete object stores need their own
    # recovery contracts; a plain Git bundle cannot attest those workspaces.
    staged = git(target, "ls-files", "--stage", "-z")
    if any(row.startswith(b"160000 ") or row.startswith(b"040000 ") for row in staged.split(b"\0")):
        raise WorktreeError("unsupported_index_entry")
    if (common / "shallow").exists() or (common / "objects/info/alternates").exists():
        raise WorktreeError("incomplete_object_store")
    info = target.stat()
    head = git(target, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    return {
        "path": str(target),
        "device": info.st_dev,
        "inode": info.st_ino,
        "git_dir": str(git_dir),
        "common_dir": str(common),
        "head": head,
        "ref": record.get("branch"),
    }


def physical_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if k not in {"head", "ref"}}


def identity_key(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
