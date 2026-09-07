# SPDX-License-Identifier: Apache-2.0
"""Private, self-contained Git recovery with exact staged and working contents.

Archives contain Git history and file bytes and may contain secrets. They are
local artifacts, never telemetry payloads. No operation contacts a remote.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from token_burn.operations import _private_dir, _sync_dir
from token_burn.scope import observed, record_stage
from token_burn.worktree.git import WorktreeError, git, identity, resolve_worktree
from token_burn.worktree.leases import Authority, LocalLeases, mutation_lock
from token_burn.worktree.snapshot import (
    _path_state,
    _verify_archive_payload,
    worktree_recovery_snapshot,
)

SCHEMA = "token_burn.recovery.v1"
ARCHIVE_FILES = frozenset({"manifest.json", "files.tar.gz", "history.bundle", "complete.json"})


@dataclass(frozen=True)
class ArchiveLimits:
    max_files: int = 10_000
    max_bytes: int = 512 * 1024 * 1024

    def validate(self) -> None:
        if type(self.max_files) is not int or not 1 <= self.max_files <= 100_000:
            raise WorktreeError("invalid_archive_limits")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 16 * 1024**3:
            raise WorktreeError("invalid_archive_limits")


def _encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n").encode()


def _write_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _private_file(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        os.close(descriptor)
        raise WorktreeError("archive_not_private")
    return descriptor


def _json_file(path: Path, *, limit: int = 16 * 1024 * 1024) -> dict:
    with os.fdopen(_private_file(path), "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise WorktreeError("archive_limit_exceeded")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise WorktreeError("archive_invalid")
    return value


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with os.fdopen(_private_file(path), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(value: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorktreeError("unsafe_archive_path")
    path = Path(value)
    if (
        not path.parts
        or path.is_absolute()
        or any(part == ".." or part.casefold() == ".git" for part in path.parts)
    ):
        raise WorktreeError("unsafe_archive_path")
    if str(path) != value:
        raise WorktreeError("unsafe_archive_path")
    return path


def _validate_snapshot(value: Any, limits: ArchiveLimits) -> dict:
    if not isinstance(value, dict) or value.get("orphan") is not False:
        raise WorktreeError("archive_invalid")
    paths = value.get("paths")
    if (
        not isinstance(paths, list)
        or len(paths) > limits.max_files
        or any(not isinstance(path, str) for path in paths)
        or sorted(set(paths)) != paths
        or not isinstance(value.get("path_hashes"), dict)
        or not isinstance(value.get("path_modes"), dict)
        or set(value["path_hashes"]) != set(paths)
        or set(value["path_modes"]) != set(paths)
        or not isinstance(value.get("index_patch"), str)
        or not isinstance(value.get("status"), str)
    ):
        raise WorktreeError("archive_invalid")
    for path in paths:
        _relative(path)
        digest, mode = value["path_hashes"][path], value["path_modes"][path]
        if digest is None:
            if mode is not None:
                raise WorktreeError("archive_invalid")
        elif (
            not isinstance(digest, str)
            or not (digest.startswith("symlink:") or _hex(digest, {64}))
            or type(mode) is not int
            or not 0 <= mode <= 0o7777
        ):
            raise WorktreeError("archive_invalid")
    payload = {k: v for k, v in value.items() if k != "content_fingerprint"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if digest != value.get("content_fingerprint"):
        raise WorktreeError("archive_invalid")
    return value


def _hex(value: Any, lengths: set[int] = frozenset({40, 64})) -> bool:
    return (
        isinstance(value, str)
        and len(value) in lengths
        and all(c in "0123456789abcdef" for c in value)
    )


def load_archive(directory: Path | str, *, limits: ArchiveLimits = ArchiveLimits()) -> dict:
    """Verify the private archive envelope and each actual retained file payload."""
    limits.validate()
    root = Path(directory).expanduser().absolute()
    _private_dir(root)
    if {p.name for p in root.iterdir()} != ARCHIVE_FILES:
        raise WorktreeError("archive_incomplete")
    descriptor = _json_file(root / "complete.json", limit=8192)
    manifest = _json_file(root / "manifest.json")
    if descriptor.get("schema_version") != SCHEMA or manifest.get("schema_version") != SCHEMA:
        raise WorktreeError("archive_invalid")
    for name in ("manifest.json", "files.tar.gz", "history.bundle"):
        if _digest(root / name) != descriptor.get("sha256", {}).get(name):
            raise WorktreeError("archive_checksum_mismatch")
    if sum((root / name).stat().st_size for name in ARCHIVE_FILES) > limits.max_bytes:
        raise WorktreeError("archive_limit_exceeded")
    snapshot = _validate_snapshot(manifest.get("snapshot"), limits)
    if any(not _hex(manifest.get(name)) for name in ("head", "staged_tree", "staged_commit")):
        raise WorktreeError("archive_invalid")
    archive_id = manifest.get("archive_id")
    if (
        not _hex(archive_id, {32})
        or manifest.get("bundle_ref") != f"refs/token-burn/recovery/{archive_id}"
    ):
        raise WorktreeError("archive_invalid")
    if (
        not isinstance(manifest.get("identity"), dict)
        or manifest["identity"].get("head") != manifest["head"]
    ):
        raise WorktreeError("archive_invalid")
    with tarfile.open(root / "files.tar.gz", "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > limits.max_files + 1 or sum(m.size for m in members) > limits.max_bytes:
            raise WorktreeError("archive_limit_exceeded")
        for member in members:
            if member.name != "MANIFEST.json":
                if not member.name.startswith("bytes/"):
                    raise WorktreeError("unsafe_archive_path")
                _relative(member.name[len("bytes/") :])
        _verify_archive_payload(archive, snapshot)
        embedded = archive.extractfile("MANIFEST.json")
        if embedded is None or embedded.read() != _encoded(manifest):
            raise WorktreeError("archive_manifest_mismatch")
    return {
        "directory": root,
        "manifest": manifest,
        "descriptor": descriptor,
        "fingerprint": hashlib.sha256(_encoded(descriptor)).hexdigest(),
    }


def _snapshot_limits(target: Path, snapshot: dict, limits: ArchiveLimits) -> None:
    if len(snapshot["paths"]) > limits.max_files:
        raise WorktreeError("archive_limit_exceeded")
    size = 0
    for name in snapshot["paths"]:
        _relative(name)
        path = target / name
        if path.exists() or path.is_symlink():
            size += path.lstat().st_size
        if size > limits.max_bytes:
            raise WorktreeError("archive_limit_exceeded")


def _index_path(target: Path) -> Path:
    raw = git(target, "rev-parse", "--path-format=absolute", "--git-path", "index")
    return Path(os.fsdecode(raw.removesuffix(b"\n")))


def _original_index(target: Path) -> tuple[bytes, int]:
    path = _index_path(target)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
        return stream.read(), mode


@observed("worktree.archive", source_parameter="worktree_path")
def create_archive(
    worktree_path: Path | str,
    output_dir: Path | str,
    *,
    authority: Authority | None = None,
    limits: ArchiveLimits = ArchiveLimits(),
) -> dict:
    """Archive one explicitly released linked worktree and prove a fresh restore."""
    limits.validate()
    target = resolve_worktree(worktree_path)
    root = Path(output_dir).expanduser().absolute()
    if root.resolve().is_relative_to(target):
        raise WorktreeError("archive_inside_target")
    authority = authority or LocalLeases()
    with mutation_lock(target):
        before = identity(target)
        approval = authority.inspect(target)
        approval.validate()
        if not approval.released:
            raise WorktreeError("active_owner_lease")
        snapshot = worktree_recovery_snapshot(target)
        _snapshot_limits(target, snapshot, limits)
        original_index, original_mode = _original_index(target)
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        _private_dir(root)
        archive_id = uuid.uuid4().hex
        ref = f"refs/token-burn/recovery/{archive_id}"
        anchored = None
        record_stage("recovery")
        try:
            with tempfile.TemporaryDirectory(prefix="token-burn-index-") as temporary:
                index = Path(temporary) / "index"
                _write_file(index, original_index)
                env = {"GIT_INDEX_FILE": str(index)}
                # write-tree reads the staged version; commit-tree does not run
                # hooks, update the owner's branch, or change the owner's index.
                tree = git(target, "write-tree", env=env).decode().strip()
                staged = (
                    git(
                        target,
                        "commit-tree",
                        tree,
                        "-p",
                        before["head"],
                        input=b"token-burn private recovery snapshot\n",
                    )
                    .decode()
                    .strip()
                )
                git(target, "update-ref", ref, staged, "0" * len(staged))
                anchored = staged
                git(target, "bundle", "create", str(root / "history.bundle"), ref)
            os.chmod(root / "history.bundle", 0o600)
            manifest = {
                "schema_version": SCHEMA,
                "archive_id": archive_id,
                "identity": before,
                "approval": asdict(approval),
                "head": before["head"],
                "staged_tree": tree,
                "staged_commit": staged,
                "bundle_ref": ref,
                "snapshot": snapshot,
            }
            payload = _encoded(manifest)
            _write_file(root / "manifest.json", payload)
            fd = os.open(
                root / "files.tar.gz", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as stream:
                with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                    info = tarfile.TarInfo("MANIFEST.json")
                    info.size, info.mode = len(payload), 0o600
                    archive.addfile(info, io.BytesIO(payload))
                    for relative in snapshot["paths"]:
                        source = target / relative
                        if source.exists() or source.is_symlink():
                            archive.add(source, arcname=f"bytes/{relative}", recursive=False)
                stream.flush()
                os.fsync(stream.fileno())
            fd = _private_file(root / "history.bundle")
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            descriptor = {
                "schema_version": SCHEMA,
                "sha256": {
                    name: _digest(root / name)
                    for name in ("manifest.json", "files.tar.gz", "history.bundle")
                },
            }
            _write_file(root / "complete.json", _encoded(descriptor))
            _sync_dir(root)
            loaded = load_archive(root, limits=limits)
            prove_restore(loaded, limits=limits)
            if (
                identity(target) != before
                or authority.inspect(target) != approval
                or worktree_recovery_snapshot(target) != snapshot
                or _original_index(target) != (original_index, original_mode)
            ):
                raise WorktreeError("target_identity_changed")
            git(target, "update-ref", "-d", ref, staged)
            anchored = None
            return {
                "ok": True,
                "archive": str(root),
                "archive_sha256": loaded["fingerprint"],
                "head": before["head"],
                "paths": len(snapshot["paths"]),
                "restore_verified": True,
            }
        except BaseException as exc:
            # Retain incomplete artifacts and any anchored recovery ref. Never
            # delete a user's recovery copy after a failure or a changed target.
            if anchored is not None:
                try:
                    exc.recovery_ref = ref
                except (AttributeError, TypeError):
                    pass
            raise


def _restore(loaded: dict, destination: Path, *, limits: ArchiveLimits) -> dict:
    manifest = loaded["manifest"]
    root = loaded["directory"]
    snapshot = manifest["snapshot"]
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    git(destination, "init", "--quiet")
    git(
        destination,
        "fetch",
        "--no-tags",
        str(root / "history.bundle"),
        f"{manifest['bundle_ref']}:refs/heads/token-burn-recovery",
    )
    recovered = git(destination, "rev-parse", "refs/heads/token-burn-recovery").decode().strip()
    if recovered != manifest["staged_commit"]:
        raise WorktreeError("archive_git_identity_mismatch")
    if (
        git(destination, "rev-parse", f"{recovered}^{{tree}}").decode().strip()
        != manifest["staged_tree"]
    ):
        raise WorktreeError("archive_git_identity_mismatch")
    if git(destination, "rev-parse", f"{recovered}^").decode().strip() != manifest["head"]:
        raise WorktreeError("archive_git_identity_mismatch")
    git(destination, "checkout", "--detach", manifest["head"])
    git(destination, "read-tree", manifest["staged_tree"])
    # Apply only validated leaf paths. Never call tar.extract/extractall on an
    # archive, and never write through an archive-provided parent symlink.
    with tarfile.open(root / "files.tar.gz", "r:gz") as archive:
        for name in sorted(snapshot["paths"], key=lambda p: (len(Path(p).parts), p), reverse=True):
            path = destination / _relative(name)
            parent = path.parent.resolve()
            if not parent.is_relative_to(destination.resolve()):
                raise WorktreeError("unsafe_archive_path")
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                # A tracked directory-to-file replacement is handled after its
                # archived deleted children have been removed. Never recurse.
                try:
                    path.rmdir()
                except OSError as exc:
                    raise WorktreeError("restore_path_conflict") from exc
        for name in sorted(snapshot["paths"], key=lambda p: (len(Path(p).parts), p)):
            expected = snapshot["path_hashes"][name]
            if expected is None:
                continue
            path = destination / _relative(name)
            if not path.parent.resolve().is_relative_to(destination.resolve()):
                raise WorktreeError("unsafe_archive_path")
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = snapshot["path_modes"][name]
            if expected.startswith("symlink:"):
                os.symlink(expected[len("symlink:") :], path)
            else:
                member = archive.getmember("bytes/" + name)
                stream = archive.extractfile(member)
                if stream is None:
                    raise WorktreeError("archive_invalid")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        output.write(chunk)
                    output.flush()
                    os.fchmod(output.fileno(), mode)
                    os.fsync(output.fileno())
    if (
        git(destination, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv").decode(
            "utf-8", "surrogateescape"
        )
        != snapshot["index_patch"]
    ):
        raise WorktreeError("restored_index_mismatch")
    for name in snapshot["paths"]:
        if _path_state(destination, name) != (
            snapshot["path_hashes"][name],
            snapshot["path_modes"][name],
        ):
            raise WorktreeError("restored_bytes_mismatch")
    git(destination, "fsck", "--full", "--no-reflogs", "--no-dangling")
    return {
        "ok": True,
        "destination": str(destination),
        "head": manifest["head"],
        "paths": len(snapshot["paths"]),
        "restore_verified": True,
    }


def prove_restore(loaded: dict, *, limits: ArchiveLimits = ArchiveLimits()) -> None:
    """Use an independent temporary object store, not the source repository."""
    with tempfile.TemporaryDirectory(prefix="token-burn-restore-proof-") as temporary:
        try:
            _restore(loaded, Path(temporary) / "restored", limits=limits)
        except WorktreeError as exc:
            raise WorktreeError("archive_restore_failed") from exc


@observed("worktree.restore", source_parameter="directory")
def restore_archive(
    directory: Path | str, destination: Path | str, *, limits: ArchiveLimits = ArchiveLimits()
) -> dict:
    loaded = load_archive(directory, limits=limits)
    target = Path(destination).expanduser().absolute()
    record_stage("recovery")
    result = _restore(loaded, target, limits=limits)
    if load_archive(directory, limits=limits)["fingerprint"] != loaded["fingerprint"]:
        raise WorktreeError("archive_changed")
    return result
