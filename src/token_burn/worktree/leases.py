# SPDX-License-Identifier: Apache-2.0
"""A local, explicit owner lease adapter. Expiry never grants removal authority.

This adapter coordinates cooperating callers on one machine. An application
with a separate owner service supplies an Authority implementing the same
read-only inspect contract; the application remains responsible for fencing
its writers and granting the removal request.
"""

from __future__ import annotations

import fcntl
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Protocol

from token_burn.operations import _private_dir, _read, _write
from token_burn.worktree.git import (
    WorktreeError,
    identity,
    identity_key,
    physical_identity,
    resolve_worktree,
)


@dataclass(frozen=True)
class Approval:
    lease_id: str
    generation: int
    released: bool

    def validate(self) -> None:
        if (
            not isinstance(self.lease_id, str)
            or not self.lease_id
            or len(self.lease_id) > 256
            or type(self.generation) is not int
            or self.generation < 1
            or type(self.released) is not bool
        ):
            raise WorktreeError("lease_state_unknown")


class Authority(Protocol):
    def inspect(self, worktree: Path) -> Approval:
        """Return current exact owner/generation/release facts, or raise to hold."""
        ...


def state_root() -> Path:
    raw = os.environ.get("TOKEN_BURN_WORKTREE_STATE_DIR")
    return (
        Path(raw).expanduser() if raw else Path.home() / ".local/state/token-burn/worktrees"
    ).absolute()


def _repo_state(worktree: Path) -> Path:
    value = identity(worktree, require_unlocked=False)
    # Separate independent clones, coordinate all worktrees of this Git store.
    root = state_root()
    _private_dir(root, create=True)
    result = root / identity_key({"common_dir": value["common_dir"]})
    _private_dir(result, create=True)
    return result


@dataclass(frozen=True)
class LockClaim:
    path: Path
    descriptor: int
    pid: int

    def require_live(self) -> None:
        try:
            actual = os.fstat(self.descriptor)
            expected = self.path.lstat()
        except (OSError, ValueError) as exc:
            raise WorktreeError("cleanup_lock_not_owned") from exc
        if self.pid != os.getpid() or (actual.st_dev, actual.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise WorktreeError("cleanup_lock_not_owned")


@contextmanager
def mutation_lock(worktree: Path) -> Iterator[LockClaim]:
    path = _repo_state(worktree) / "mutation.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise WorktreeError("cleanup_lock_not_private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorktreeError("cleanup_mutex_contended") from exc
        yield LockClaim(path, descriptor, os.getpid())
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class LocalLeases:
    def _path(self, worktree: Path) -> Path:
        return _repo_state(worktree) / (identity_key({"path": str(worktree.resolve())}) + ".json")

    def _record(self, worktree: Path) -> dict:
        value = _read(self._path(worktree))
        expected = physical_identity(identity(worktree, require_unlocked=False))
        if (
            value.get("schema_version") != "token_burn.lease.v1"
            or value.get("physical_identity") != expected
            or not isinstance(value.get("owner_id"), str)
            or not value["owner_id"]
        ):
            raise WorktreeError("lease_state_unknown")
        self._approval(value).validate()
        return value

    @staticmethod
    def _approval(value: dict) -> Approval:
        return Approval(value.get("lease_id"), value.get("generation"), value.get("released"))

    def inspect(self, worktree: Path) -> Approval:
        try:
            return self._approval(self._record(worktree))
        except (OSError, ValueError, KeyError) as exc:
            raise WorktreeError("lease_state_unknown") from exc

    def claim(self, worktree: Path | str, *, owner_id: str) -> Approval:
        target = resolve_worktree(worktree)
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 256:
            raise WorktreeError("invalid_owner_id")
        with mutation_lock(target):
            path = self._path(target)
            previous = None
            try:
                previous = self._record(target)
            except FileNotFoundError:
                pass
            if previous and not previous["released"]:
                raise WorktreeError("active_owner_lease")
            approval = Approval(
                uuid.uuid4().hex, previous["generation"] + 1 if previous else 1, False
            )
            _write(
                path,
                {
                    "schema_version": "token_burn.lease.v1",
                    "owner_id": owner_id,
                    "physical_identity": physical_identity(identity(target)),
                    **asdict(approval),
                },
                replace=previous is not None,
            )
            return approval

    def release(self, worktree: Path | str, *, lease_id: str) -> Approval:
        target = resolve_worktree(worktree)
        with mutation_lock(target):
            value = self._record(target)
            if value["lease_id"] != lease_id:
                raise WorktreeError("lease_owner_changed")
            if value["released"]:
                return self._approval(value)
            value.update(released=True, generation=value["generation"] + 1)
            _write(self._path(target), value, replace=True)
            return self._approval(value)
