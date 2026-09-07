# SPDX-License-Identifier: Apache-2.0
"""Bounded JSON and explicit private files for the local efficiency commands."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ContractError(ValueError):
    """An input does not satisfy a public contract. Messages contain no input values."""


def encoded(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContractError("invalid_json_value") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def read_json(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> Any:
    with Path(path).open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ContractError("json_input_too_large")
    try:
        return json.loads(
            data.decode("utf-8"), parse_constant=_invalid_constant, object_pairs_hook=_unique_object
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContractError("invalid_json_input") from exc


def _invalid_constant(value: str) -> None:
    raise ContractError("nonfinite_json_number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_json_key")
        result[key] = value
    return result


def private_directory(path: Path) -> Path:
    path = Path(path).absolute()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    facts = path.lstat()
    if (
        not stat.S_ISDIR(facts.st_mode)
        or facts.st_uid != os.getuid()
        or stat.S_IMODE(facts.st_mode) & 0o077
    ):
        raise ContractError("state_directory_not_private")
    return path


def write_json(path: Path, value: Any, *, replace: bool = False) -> str:
    """Publish complete mode-0600 JSON atomically; refuse an existing report."""
    path = Path(path).absolute()
    data = encoded(value)
    fd, temporary = tempfile.mkstemp(prefix=".token-burn-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return hashlib.sha256(data).hexdigest()


@contextmanager
def state_lock(path: Path) -> Iterator[Path]:
    """A foreground collector is the sole writer. Busy never means stale."""
    directory = private_directory(path)
    fd = os.open(directory / "collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        facts = os.fstat(fd)
        if (
            not stat.S_ISREG(facts.st_mode)
            or facts.st_uid != os.getuid()
            or stat.S_IMODE(facts.st_mode) & 0o077
            or facts.st_nlink != 1
        ):
            raise ContractError("invalid_collector_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError("collector_busy") from exc
        yield directory
    finally:
        os.close(fd)
