# SPDX-License-Identifier: Apache-2.0
"""Byte-accurate worktree snapshots. No cleanup or network effects."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
from pathlib import Path
from typing import Any

from token_burn.worktree.git import git


class RecoveryError(RuntimeError):
    def __init__(self, reason_code: str, detail: str | None = None) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(reason_code if detail is None else f"{reason_code}: {detail}")


def _git(repo: Path, *args: str) -> str:
    return git(repo, *args).decode("utf-8", "surrogateescape")


def _status_paths(worktree: Path) -> tuple[str, list[str]]:
    # Status and diff deliberately hide these tracked paths. A status-based
    # archive cannot attest their current bytes, so preserve the whole target.
    tracked = _git(worktree, "ls-files", "-v", "-z")
    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in tracked.split("\0")):
        raise RecoveryError("hidden_index_state", "tracked paths are hidden from status")
    status = _git(
        worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    # Git-clean does not imply identical on-disk bytes or modes: filters,
    # line-ending conversion and core.filemode can hide meaningful differences.
    # Retain every tracked leaf, plus changed/deleted/untracked/ignored paths.
    paths: list[str] = [entry[2:] for entry in tracked.split("\0") if entry]
    entries = iter(status.split("\0"))
    for raw in entries:
        if not raw:
            continue
        paths.append(raw[3:])
        # In -z form the destination comes first, then the original path.
        if "R" in raw[:2] or "C" in raw[:2]:
            paths.append(next(entries))
    # Git's ordinary status omits ignored files even when force removal will
    # delete them. Include their bytes under the same churn/security policy.
    ignored = _git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    paths.extend(path for path in ignored.split("\0") if path)
    return status, sorted(set(paths))


def _path_state(target: Path, relative: str) -> tuple[str | None, int | None]:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RecoveryError("dirty_archive_failed", "unsafe recovery path")
    source = target / path
    if source.parent.resolve() != target.resolve() and not source.parent.resolve().is_relative_to(
        target.resolve()
    ):
        raise RecoveryError("dirty_archive_failed", "recovery path escapes worktree")
    try:
        info = source.lstat()
    except FileNotFoundError:
        return None, None
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return "symlink:" + os.readlink(source), mode
    if not stat.S_ISREG(info.st_mode):
        raise RecoveryError("dirty_archive_failed", "unsupported dirty file type")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryError("dirty_archive_failed", "dirty file type changed")
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())

    def identity(value: os.stat_result) -> tuple:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_mode,
        )

    if (
        identity(info) != identity(before)
        or identity(before) != identity(after)
        or identity(after) != identity(source.lstat())
    ):
        raise RecoveryError("dirty_archive_failed", "dirty bytes changed during snapshot")
    return digest.hexdigest(), mode


def worktree_recovery_snapshot(worktree: Path | str) -> dict[str, Any]:
    """Snapshot exact working file bytes, modes and index state; Git errors hold."""
    target = Path(worktree).resolve()
    try:
        top = _git(target, "rev-parse", "--show-toplevel").removesuffix("\n")
    except RecoveryError:
        # A registered but broken checkout must not masquerade as an orphan.
        if (target / ".git").exists() or (target / ".git").is_symlink():
            raise
        top = ""
    orphan = not top or Path(top).resolve() != target
    if orphan:
        paths = sorted(
            str(p.relative_to(target)) for p in target.rglob("*") if p.is_file() or p.is_symlink()
        )
        status = "".join(f"?? {p}\0" for p in paths)
        index_patch = ""
    else:
        status, paths = _status_paths(target)
        index_patch = _git(target, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv")
    hashes: dict[str, str | None] = {}
    modes: dict[str, int | None] = {}
    try:
        for relative in paths:
            hashes[relative], modes[relative] = _path_state(target, relative)
    except OSError as exc:
        raise RecoveryError("dirty_archive_failed", "dirty snapshot unavailable") from exc
    snapshot = {
        "status": status,
        "paths": paths,
        "path_hashes": hashes,
        "path_modes": modes,
        "orphan": orphan,
        "index_patch": index_patch,
    }
    snapshot["content_fingerprint"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return snapshot


def _verify_archive_payload(archive: tarfile.TarFile, snapshot: dict[str, Any]) -> None:
    expected_names = {"MANIFEST.json"} | {
        f"bytes/{name}" for name, value in snapshot["path_hashes"].items() if value is not None
    }
    members = archive.getmembers()
    if len(members) != len(expected_names) or {member.name for member in members} != expected_names:
        raise RecoveryError("dirty_archive_failed", "archive entry inventory differs")
    for relative, expected in snapshot["path_hashes"].items():
        if expected is None:
            continue
        member = archive.getmember(f"bytes/{relative}")
        if stat.S_IMODE(member.mode) != snapshot["path_modes"][relative]:
            raise RecoveryError("dirty_archive_failed", "archive file mode differs")
        if expected.startswith("symlink:"):
            if not member.issym() or member.linkname != expected.removeprefix("symlink:"):
                raise RecoveryError("dirty_archive_failed", "archive symlink differs")
            continue
        if not member.isfile() and not (
            member.islnk() and member.linkname in expected_names - {"MANIFEST.json"}
        ):
            raise RecoveryError("dirty_archive_failed", "archive entry type differs")
        stream = archive.extractfile(member)
        if stream is None:
            raise RecoveryError("dirty_archive_failed", "archive file missing")
        digest = hashlib.sha256()
        with stream:
            for chunk in iter(lambda stream=stream: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise RecoveryError("dirty_archive_failed", "archive file bytes differ")
