# SPDX-License-Identifier: Apache-2.0
"""Explicit local ownership and recovery commands for one linked worktree."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from token_burn.arguments import ArgumentParser
from token_burn.evidence import summary
from token_burn.worktree.archive import ArchiveLimits, create_archive, load_archive, restore_archive
from token_burn.worktree.guards import acquire_permit, remove_worktree
from token_burn.worktree.leases import LocalLeases


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="token-burn worktree", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    claim = commands.add_parser("claim", help="Record the owner before starting work")
    claim.add_argument("target", type=Path)
    claim.add_argument("--owner", required=True)
    release = commands.add_parser("release", help="Explicitly release the exact owner lease")
    release.add_argument("target", type=Path)
    release.add_argument("--lease-id", required=True)
    state = commands.add_parser("lease", help="Inspect current local ownership")
    state.add_argument("target", type=Path)
    archive = commands.add_parser(
        "archive", help="Save a released worktree and prove a fresh restore"
    )
    archive.add_argument("target", type=Path)
    archive.add_argument("--output-dir", type=Path, required=True)
    restore = commands.add_parser("restore", help="Restore into a new independent repository")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--destination", type=Path, required=True)
    inspect = commands.add_parser("inspect", help="Verify a private archive without restoring it")
    inspect.add_argument("archive", type=Path)
    remove = commands.add_parser(
        "remove", help="Preview removal; --apply performs the guarded action"
    )
    remove.add_argument("target", type=Path)
    remove.add_argument("--archive", type=Path, required=True)
    remove.add_argument("--apply", action="store_true")
    for command in (archive, restore, inspect, remove):
        command.add_argument("--max-files", type=int, default=10_000)
        command.add_argument("--max-bytes", type=int, default=512 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        limits = ArchiveLimits(
            getattr(args, "max_files", 10_000), getattr(args, "max_bytes", 512 * 1024 * 1024)
        )
        if args.command == "claim":
            result = {"ok": True, **asdict(LocalLeases().claim(args.target, owner_id=args.owner))}
        elif args.command == "release":
            result = {
                "ok": True,
                **asdict(LocalLeases().release(args.target, lease_id=args.lease_id)),
            }
        elif args.command == "lease":
            result = {"ok": True, **asdict(LocalLeases().inspect(args.target))}
        elif args.command == "archive":
            result = create_archive(args.target, args.output_dir, limits=limits)
        elif args.command == "restore":
            result = restore_archive(args.archive, args.destination, limits=limits)
        elif args.command == "inspect":
            loaded = load_archive(args.archive, limits=limits)
            result = {
                "ok": True,
                "archive_sha256": loaded["fingerprint"],
                "head": loaded["manifest"]["head"],
                "paths": len(loaded["manifest"]["snapshot"]["paths"]),
            }
        elif args.apply:
            result = remove_worktree(args.target, args.archive, limits=limits)
        else:
            with acquire_permit(args.target, args.archive, limits=limits):
                result = {
                    "ok": True,
                    "preview": True,
                    "removed": False,
                    "next": "Repeat with --apply to perform a fresh guarded removal.",
                }
        print(summary(result))
        return 0
    except Exception as exc:
        result = {
            "ok": False,
            "error_type": type(exc).__name__,
            "reason_code": getattr(exc, "reason_code", "worktree_operation_failed"),
        }
        if getattr(exc, "operation", None):
            result["operation"] = exc.operation
        if getattr(exc, "recovery_ref", None):
            result["retained_recovery_ref"] = exc.recovery_ref
        print(json.dumps(result, ensure_ascii=True))
        return 1
