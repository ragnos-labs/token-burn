"""Actual disposable Git worktrees, complete restoration, and refusal controls."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from token_burn.worktree.archive import create_archive, restore_archive
from token_burn.worktree.git import WorktreeError, git, identity
from token_burn.worktree.guards import acquire_permit, remove_worktree
from token_burn.worktree.leases import LocalLeases
from token_burn.worktree.snapshot import worktree_recovery_snapshot


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "file.txt").write_text("original\n")
    (repo / "deleted.txt").write_text("keep in history\n")
    (repo / ".gitignore").write_text("ignored/\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "initial fixture")
    target = tmp_path / "worktree"
    git(repo, "worktree", "add", "--quiet", "-b", "topic", str(target))
    yield repo, target


def released(target):
    leases = LocalLeases()
    owner = leases.claim(target, owner_id="test-owner")
    return leases.release(target, lease_id=owner.lease_id)


def dirty(target):
    (target / "file.txt").write_text("staged\n")
    git(target, "add", "file.txt")
    (target / "file.txt").write_text("working\n")
    (target / "file.txt").chmod(0o755)
    (target / "deleted.txt").unlink()
    (target / "ignored").mkdir()
    (target / "ignored/private.bin").write_bytes(b"\0\xffprivate bytes")
    (target / "untracked.txt").write_text("not in Git\n")
    (target / "link").symlink_to("file.txt")


def test_remove_and_restore_exact_staged_working_and_ignored_content(worktree, tmp_path):
    repo, target = worktree
    dirty(target)
    released(target)
    before = worktree_recovery_snapshot(target)
    original_head = git(target, "rev-parse", "HEAD")
    index = Path(identity(target)["git_dir"]) / "index"
    original_index = index.read_bytes()
    archive = tmp_path / "private-archive"
    result = create_archive(target, archive)
    assert result["restore_verified"]
    assert git(target, "rev-parse", "HEAD") == original_head
    assert index.read_bytes() == original_index
    assert not git(repo, "for-each-ref", "refs/token-burn/recovery")
    removed = remove_worktree(target, archive)
    assert removed["removed"] and not target.exists()
    assert git(repo, "rev-parse", "refs/heads/topic") == original_head
    restored = tmp_path / "fresh-restored"
    assert restore_archive(archive, restored)["restore_verified"]
    assert worktree_recovery_snapshot(restored) == before
    assert git(restored, "show", ":file.txt") == b"staged\n"
    assert (restored / "file.txt").read_bytes() == b"working\n"
    assert stat.S_IMODE((restored / "file.txt").stat().st_mode) == 0o755
    assert not (restored / "deleted.txt").exists()
    assert os.readlink(restored / "link") == "file.txt"
    assert git(restored, "rev-parse", "HEAD") == original_head


@pytest.mark.parametrize(
    "name",
    [
        "carriage\rreturn.txt",
        "CRLF\r\nname",
        "line\nname",
        "tab\tname",
        "space name",
        "-leading",
        "unicode-雪",
        "back\\slash",
    ],
)
def test_unusual_names_survive_real_removal(worktree, tmp_path, name):
    _, target = worktree
    (target / name).write_bytes(b"\xff\0working")
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    remove_worktree(target, archive)
    restored = tmp_path / "restored"
    restore_archive(archive, restored)
    assert (restored / name).read_bytes() == b"\xff\0working"


def test_active_lease_and_primary_checkout_are_preserved(worktree, tmp_path):
    repo, target = worktree
    owner = LocalLeases().claim(target, owner_id="active")
    with pytest.raises(WorktreeError, match="active_owner_lease"):
        create_archive(target, tmp_path / "archive")
    with pytest.raises(WorktreeError, match="primary_checkout_protected"):
        LocalLeases().claim(repo, owner_id="wrong-target")
    assert target.exists() and repo.exists()
    assert LocalLeases().inspect(target).lease_id == owner.lease_id


def test_same_status_changed_bytes_invalidate_recovery(worktree, tmp_path):
    _, target = worktree
    dirty(target)
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    status = git(target, "status", "--porcelain", "-z")
    (target / "file.txt").write_text("changed after archive\n")
    assert git(target, "status", "--porcelain", "-z") == status
    with pytest.raises(WorktreeError, match="target_identity_changed"):
        remove_worktree(target, archive)
    assert (target / "file.txt").read_text() == "changed after archive\n"


def test_released_permit_cannot_borrow_new_lock(worktree, tmp_path):
    _, target = worktree
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    old = acquire_permit(target, archive)
    old.release()
    with acquire_permit(target, archive):
        with pytest.raises(WorktreeError, match="mutation_permit_released"):
            old.remove(target)
    assert target.exists()


def test_one_permit_one_concurrent_removal(worktree, tmp_path):
    _, target = worktree
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    with acquire_permit(target, archive) as permit:

        def attempt():
            try:
                return permit.remove(target)["removed"]
            except WorktreeError as exc:
                return exc.reason_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count(True) == 1
    assert results.count("mutation_permit_consumed") == 1


def test_new_owner_cannot_reuse_old_archive(worktree, tmp_path):
    _, target = worktree
    old = released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    new = released(target)
    assert new.lease_id != old.lease_id and new.generation > old.generation
    with pytest.raises(WorktreeError, match="target_identity_changed"):
        remove_worktree(target, archive)
    assert target.exists()


@pytest.mark.parametrize(
    "field,value",
    [("lease_id", None), ("generation", True), ("released", "true"), ("physical_identity", {})],
)
def test_malformed_authority_never_grants_removal(worktree, tmp_path, field, value):
    _, target = worktree
    released(target)
    store = LocalLeases()
    path = store._path(target)
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(WorktreeError, match="lease_state_unknown"):
        store.inspect(target)
    with pytest.raises((WorktreeError, ValueError)):
        store.claim(target, owner_id="must-not-repair")
    assert json.loads(path.read_text()) == record


def test_archive_tampering_never_allows_removal(worktree, tmp_path):
    _, target = worktree
    dirty(target)
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    (archive / "history.bundle").write_bytes(b"invalid Git bundle")
    complete = json.loads((archive / "complete.json").read_text())
    complete["sha256"]["history.bundle"] = hashlib.sha256(b"invalid Git bundle").hexdigest()
    (archive / "complete.json").write_text(json.dumps(complete))
    with pytest.raises(WorktreeError):
        remove_worktree(target, archive)
    assert target.exists()


def test_nested_live_cwd_blocks_removal(worktree, tmp_path):
    _, target = worktree
    nested = target / "deep/a/b"
    nested.mkdir(parents=True)
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], cwd=nested)
    try:
        with pytest.raises(WorktreeError, match="active_process"):
            remove_worktree(target, archive)
        assert target.exists()
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_restore_never_overwrites_existing_destination(worktree, tmp_path):
    _, target = worktree
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("untouched")
    with pytest.raises(FileExistsError):
        restore_archive(archive, existing)
    assert (existing / "keep").read_text() == "untouched"


def test_archive_limits_and_output_in_target_hold(worktree, tmp_path):
    from token_burn.worktree.archive import ArchiveLimits

    _, target = worktree
    released(target)
    (target / "too-large").write_bytes(b"x" * 100)
    with pytest.raises(WorktreeError, match="archive_limit_exceeded"):
        create_archive(target, tmp_path / "archive", limits=ArchiveLimits(max_bytes=50))
    with pytest.raises(WorktreeError, match="archive_inside_target"):
        create_archive(target, target / "nested-archive")
    assert target.exists()


@pytest.mark.parametrize("change", ["mode_hidden_by_config", "normalized_line_endings"])
def test_git_clean_does_not_hide_actual_working_bytes_or_modes(worktree, tmp_path, change):
    _, target = worktree
    path = target / "file.txt"
    if change == "mode_hidden_by_config":
        git(target, "config", "core.filemode", "false")
        path.chmod(0o755)
    else:
        git(target, "config", "core.autocrlf", "true")
        path.write_bytes(b"original\r\n")
        git(target, "add", "file.txt")
    assert git(target, "status", "--porcelain", "-z") == b""
    expected = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    restored = tmp_path / "restored"
    restore_archive(archive, restored)
    actual = restored / "file.txt"
    assert (actual.read_bytes(), stat.S_IMODE(actual.stat().st_mode)) == expected


def test_low_level_removal_requires_durable_start(worktree, tmp_path, monkeypatch):
    _, target = worktree
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    with acquire_permit(target, archive) as permit:
        invalid_journal = tmp_path / "not-private"
        invalid_journal.mkdir(mode=0o755)
        monkeypatch.setenv("TOKEN_BURN_STATE_DIR", str(invalid_journal))
        with pytest.raises(ValueError, match="journal_directory_not_private"):
            permit.remove(target)
    assert target.exists()


def test_replaced_lock_inode_invalidates_permit(worktree, tmp_path):
    _, target = worktree
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    with acquire_permit(target, archive) as permit:
        path = permit._claim.path
        path.rename(path.with_name("retained-lock"))
        path.touch(mode=0o600)
        with pytest.raises(WorktreeError, match="cleanup_lock_not_owned"):
            permit.remove(target)
    assert target.exists()


def test_external_authority_new_owner_with_same_generation_is_rejected(worktree, tmp_path):
    _, target = worktree
    from token_burn.worktree.leases import Approval

    class ExternalAuthority:
        value = Approval("owner-one", 2, True)

        def inspect(self, path):
            assert path == target
            return self.value

    authority = ExternalAuthority()
    archive = tmp_path / "archive"
    create_archive(target, archive, authority=authority)
    with acquire_permit(target, archive, authority=authority) as permit:
        authority.value = Approval("owner-two", 2, True)
        with pytest.raises(WorktreeError, match="lease_owner_changed"):
            permit.remove(target)
    assert target.exists()


def test_another_process_cannot_change_local_ownership_under_permit(worktree, tmp_path):
    _, target = worktree
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    with acquire_permit(target, archive):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "token_burn",
                "worktree",
                "claim",
                str(target),
                "--owner",
                "competitor",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason_code"] == "cleanup_mutex_contended"
    assert target.exists()


def test_symbolic_target_alias_is_not_an_explicit_removal_target(worktree, tmp_path):
    _, target = worktree
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(WorktreeError, match="unsafe_target"):
        LocalLeases().claim(alias, owner_id="alias-owner")
    assert alias.is_symlink() and target.exists()


def test_local_git_command_filters_hold_before_execution(worktree, tmp_path):
    _, target = worktree
    marker = tmp_path / "filter-ran"
    git(target, "config", "filter.danger.clean", f"touch {marker}")
    (target / ".gitattributes").write_text("file.txt filter=danger\n")
    (target / "file.txt").write_text("changed\n")
    with pytest.raises(WorktreeError, match="configured_filter_requires_adapter"):
        LocalLeases().claim(target, owner_id="filter-owner")
    assert not marker.exists()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_index_state_holds(worktree, tmp_path, flag):
    _, target = worktree
    released(target)
    git(target, "update-index", flag, "file.txt")
    from token_burn.worktree.snapshot import RecoveryError

    with pytest.raises(RecoveryError, match="hidden_index_state"):
        create_archive(target, tmp_path / "archive")
    assert target.exists()


def test_rename_and_staged_delete_restore(worktree, tmp_path):
    _, target = worktree
    git(target, "mv", "file.txt", "renamed.txt")
    git(target, "rm", "deleted.txt")
    (target / "renamed.txt").write_bytes(b"working after rename\0\xff")
    released(target)
    before = worktree_recovery_snapshot(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    remove_worktree(target, archive)
    restored = tmp_path / "restored"
    restore_archive(archive, restored)
    assert worktree_recovery_snapshot(restored) == before
    assert git(restored, "show", ":renamed.txt") == b"original\n"
    assert not (restored / "file.txt").exists()


@pytest.mark.parametrize("corruption", ["bytes", "mode"])
def test_recomputed_archive_digest_cannot_hide_bad_payload(worktree, tmp_path, corruption):
    import io
    import tarfile

    from token_burn.worktree.snapshot import RecoveryError

    _, target = worktree
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    before = worktree_recovery_snapshot(target)
    replacement = tmp_path / "replacement.tar.gz"
    with tarfile.open(archive / "files.tar.gz", "r:gz") as original:
        with tarfile.open(replacement, "w:gz") as changed:
            for member in original.getmembers():
                stream = original.extractfile(member) if member.isfile() or member.islnk() else None
                raw = stream.read() if stream else None
                if member.name == "bytes/file.txt":
                    if corruption == "bytes":
                        raw = b"incorrect bytes"
                        member.size = len(raw)
                    else:
                        member.mode ^= 0o100
                changed.addfile(member, io.BytesIO(raw) if raw is not None else None)
    replacement.chmod(0o600)
    replacement.replace(archive / "files.tar.gz")
    descriptor = json.loads((archive / "complete.json").read_text())
    descriptor["sha256"]["files.tar.gz"] = hashlib.sha256(
        (archive / "files.tar.gz").read_bytes()
    ).hexdigest()
    (archive / "complete.json").write_text(json.dumps(descriptor))
    with pytest.raises(RecoveryError):
        remove_worktree(target, archive)
    assert worktree_recovery_snapshot(target) == before


@pytest.mark.parametrize("unsafe", [".GiT/config", "."])
def test_forged_metadata_paths_are_rejected_before_restore_destination(worktree, tmp_path, unsafe):
    import io
    import tarfile

    _, target = worktree
    released(target)
    archive = tmp_path / "archive"
    create_archive(target, archive)
    manifest = json.loads((archive / "manifest.json").read_text())
    snapshot = manifest["snapshot"]
    payload = b"synthetic metadata overwrite"
    snapshot["paths"] = sorted([*snapshot["paths"], unsafe])
    snapshot["path_hashes"][unsafe] = hashlib.sha256(payload).hexdigest()
    snapshot["path_modes"][unsafe] = 0o600
    snapshot["content_fingerprint"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in snapshot.items() if k != "content_fingerprint"},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    encoded = (
        json.dumps(manifest, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    (archive / "manifest.json").write_bytes(encoded)
    replacement = tmp_path / "replacement.tar.gz"
    with tarfile.open(archive / "files.tar.gz", "r:gz") as original:
        with tarfile.open(replacement, "w:gz") as changed:
            for member in original.getmembers():
                stream = original.extractfile(member) if member.isfile() or member.islnk() else None
                raw = stream.read() if stream else None
                if member.name == "MANIFEST.json":
                    raw = encoded
                    member.size = len(raw)
                changed.addfile(member, io.BytesIO(raw) if raw is not None else None)
            member = tarfile.TarInfo("bytes/" + unsafe)
            member.size, member.mode = len(payload), 0o600
            changed.addfile(member, io.BytesIO(payload))
    replacement.chmod(0o600)
    replacement.replace(archive / "files.tar.gz")
    descriptor = json.loads((archive / "complete.json").read_text())
    for name in ("manifest.json", "files.tar.gz"):
        descriptor["sha256"][name] = hashlib.sha256((archive / name).read_bytes()).hexdigest()
    (archive / "complete.json").write_text(json.dumps(descriptor))
    destination = tmp_path / "restored"
    with pytest.raises(WorktreeError, match="unsafe_archive_path"):
        restore_archive(archive, destination)
    assert not destination.exists()
