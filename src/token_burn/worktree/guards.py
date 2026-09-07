# SPDX-License-Identifier: Apache-2.0
"""Single-use removal permits bound to current ownership, bytes and recovery."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from token_burn.scope import observed, record_stage
from token_burn.worktree.archive import ArchiveLimits, load_archive, prove_restore
from token_burn.worktree.git import (
    WorktreeError,
    git,
    identity,
    registered_worktrees,
    resolve_worktree,
)
from token_burn.worktree.leases import Authority, LocalLeases, mutation_lock
from token_burn.worktree.liveness import require_idle
from token_burn.worktree.snapshot import worktree_recovery_snapshot


class RemovalPermit:
    """A held local mutex plus exact evidence, consumed by the removal itself.

    Construct via acquire_permit and close with a context manager. Releasing a
    permit permanently invalidates it, even if another caller later holds the
    same lock. Failed attempts also consume their permit.
    """

    def __init__(
        self, target: Path, archive: Path, authority: Authority, limits: ArchiveLimits
    ) -> None:
        self.target = target
        self.archive = archive
        self.authority = authority
        self.limits = limits
        self.pid = os.getpid()
        self.issued_at = time.monotonic()
        self.consumed = False
        self.released = False
        self._state_lock = threading.RLock()
        self._lock = mutation_lock(target)
        self._claim = self._lock.__enter__()
        try:
            self.identity = identity(target)
            self.approval = authority.inspect(target)
            self.approval.validate()
            if not self.approval.released:
                raise WorktreeError("active_owner_lease")
            loaded = load_archive(archive, limits=limits)
            self.snapshot = loaded["manifest"]["snapshot"]
            self.archive_fingerprint = loaded["fingerprint"]
            if (
                loaded["manifest"]["identity"] != self.identity
                or loaded["manifest"].get("approval") != asdict(self.approval)
                or worktree_recovery_snapshot(target) != self.snapshot
            ):
                raise WorktreeError("target_identity_changed")
            require_idle(target)
            prove_restore(loaded, limits=limits)
            self._recheck()
        except BaseException:
            self.release()
            raise

    def _recheck(self) -> None:
        if self.released or self.pid != os.getpid():
            raise WorktreeError("mutation_permit_released")
        if self.consumed:
            raise WorktreeError("mutation_permit_consumed")
        if time.monotonic() - self.issued_at > 600:
            raise WorktreeError("mutation_permit_expired")
        self._claim.require_live()
        if identity(self.target) != self.identity:
            raise WorktreeError("target_identity_changed")
        current = self.authority.inspect(self.target)
        current.validate()
        if current != self.approval or not current.released:
            raise WorktreeError("lease_owner_changed")
        loaded = load_archive(self.archive, limits=self.limits)
        if loaded["fingerprint"] != self.archive_fingerprint:
            raise WorktreeError("archive_changed")
        if worktree_recovery_snapshot(self.target) != self.snapshot:
            raise WorktreeError("target_identity_changed")
        if (Path(self.identity["git_dir"]) / "index.lock").exists():
            raise WorktreeError("git_index_locked")

    def release(self) -> None:
        with self._state_lock:
            if not self.released:
                self.released = True
                self._lock.__exit__(None, None, None)

    def __enter__(self) -> RemovalPermit:
        if self.released:
            raise WorktreeError("mutation_permit_released")
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()

    @observed("worktree.cleanup", source_parameter="worktree_path")
    def remove(self, worktree_path: Path | str) -> dict:
        with self._state_lock:
            if self.released or self.pid != os.getpid():
                raise WorktreeError("mutation_permit_released")
            if self.consumed:
                raise WorktreeError("mutation_permit_consumed")
            if resolve_worktree(worktree_path) != self.target:
                raise WorktreeError("mutation_permit_target_mismatch")
            record_stage("recheck")
            require_idle(self.target)
            self._recheck()
            self.consumed = True
            record_stage("remove")
            common = Path(self.identity["common_dir"])
            git(common, "worktree", "remove", "--force", "--", str(self.target))
            if self.target.exists() or self.target in registered_worktrees(common):
                raise WorktreeError("removal_not_verified")
            return {
                "ok": True,
                "removed": True,
                "worktree": str(self.target),
                "archive": str(self.archive),
                "archive_sha256": self.archive_fingerprint,
                "branch_deleted": False,
                "restore_verified": True,
            }


def acquire_permit(
    worktree_path: Path | str,
    archive: Path | str,
    *,
    authority: Authority | None = None,
    limits: ArchiveLimits = ArchiveLimits(),
) -> RemovalPermit:
    return RemovalPermit(
        resolve_worktree(worktree_path),
        Path(archive).expanduser().absolute(),
        authority or LocalLeases(),
        limits,
    )


@observed("worktree.cleanup", source_parameter="worktree_path")
def remove_worktree(
    worktree_path: Path | str,
    archive: Path | str,
    *,
    authority: Authority | None = None,
    limits: ArchiveLimits = ArchiveLimits(),
) -> dict:
    """Remove one explicitly requested linked worktree, keeping branches and archives."""
    record_stage("admission")
    with acquire_permit(worktree_path, archive, authority=authority, limits=limits) as permit:
        return permit.remove(worktree_path)
