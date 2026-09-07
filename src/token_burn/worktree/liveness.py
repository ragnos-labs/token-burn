# SPDX-License-Identifier: Apache-2.0
"""Fail-closed process-cwd inspection for one linked worktree."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from token_burn.worktree.git import WorktreeError


def _matches(cwd: Path, target: Path) -> bool:
    return cwd == target or target in cwd.parents


def _decode_lsof(raw: bytes) -> str:
    """Decode lsof's C escapes without conflating a newline and literal '\\n'."""
    escapes = {
        ord("a"): 7,
        ord("b"): 8,
        ord("t"): 9,
        ord("n"): 10,
        ord("v"): 11,
        ord("f"): 12,
        ord("r"): 13,
        ord("\\"): 92,
    }
    result = bytearray()
    index = 0
    while index < len(raw):
        value = raw[index]
        if value != 92:
            result.append(value)
            index += 1
            continue
        index += 1
        if index >= len(raw):
            raise WorktreeError("liveness_unknown")
        value = raw[index]
        if value in escapes:
            result.append(escapes[value])
            index += 1
        elif value == ord("x") and index + 2 < len(raw):
            digits = raw[index + 1 : index + 3]
            if any(char not in b"0123456789abcdefABCDEF" for char in digits):
                raise WorktreeError("liveness_unknown")
            result.append(int(digits, 16))
            index += 3
        else:
            raise WorktreeError("liveness_unknown")
    return os.fsdecode(bytes(result))


def require_idle(worktree: Path) -> None:
    target = worktree.resolve(strict=True)
    try:
        if sys.platform.startswith("linux"):
            _linux(target)
        elif sys.platform == "darwin":
            _macos(target)
        else:
            raise WorktreeError("liveness_unknown")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        if isinstance(exc, WorktreeError):
            raise
        raise WorktreeError("liveness_unknown") from exc


def _linux(target: Path) -> None:
    proc = Path("/proc")
    if not proc.is_dir():
        raise WorktreeError("liveness_unknown")
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            cwd = (process / "cwd").resolve(strict=True)
        except FileNotFoundError:
            continue  # exited process or kernel thread without a cwd
        if _matches(cwd, target):
            raise WorktreeError("active_process")


def _macos(target: Path) -> None:
    result = subprocess.run(
        ["lsof", "-a", "-d", "cwd", "-F0pn"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode not in (0, 1) or result.stderr:
        raise WorktreeError("liveness_unknown")
    if not result.stdout:
        if result.returncode == 1:
            return
        raise WorktreeError("liveness_unknown")
    seen_pid = False
    seen_cwd = False
    for field in result.stdout.split(b"\0"):
        field = field.lstrip(b"\n")
        if not field:
            continue
        if field.startswith(b"p"):
            if seen_pid and not seen_cwd:
                raise WorktreeError("liveness_unknown")
            pid = int(field[1:])
            if pid <= 0:
                raise WorktreeError("liveness_unknown")
            seen_pid, seen_cwd = True, False
        elif field.startswith(b"n"):
            if not seen_pid:
                raise WorktreeError("liveness_unknown")
            raw = _decode_lsof(field[1:])
            if not Path(raw).is_absolute():
                raise WorktreeError("liveness_unknown")
            seen_cwd = True
            if _matches(Path(raw).resolve(), target):
                raise WorktreeError("active_process")
    if not seen_pid or not seen_cwd:
        raise WorktreeError("liveness_unknown")
    # Retain the independent root-directory open-file hold on macOS.
    handles = subprocess.run(
        ["lsof", "-t", "+d", str(target)], capture_output=True, timeout=10, check=False
    )
    if handles.returncode not in (0, 1) or handles.stderr:
        raise WorktreeError("liveness_unknown")
    if handles.stdout.strip():
        raise WorktreeError("active_process")
