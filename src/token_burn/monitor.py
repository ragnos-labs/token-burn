# SPDX-License-Identifier: Apache-2.0
"""Read-only, bounded inventory of private operation journals.

The existing status/read methods acquire a create-if-missing blocking lock.
Inventory instead opens that existing lock read-only/nonblocking and applies
the same owner and delivery semantics to a bounded event snapshot. No journal
file, exporter, scheduler, or backend is mutated. Counts cover the observed
inventory only; incomplete scans explicitly report incomplete, never healthy.
"""

from __future__ import annotations

import fcntl
import os
import stat
import time
from pathlib import Path
from typing import Any

from token_burn.operations import (
    OUTCOMES,
    _id,
    _private_dir,
    _process_identity,
    _read,
    _root,
    _validate_envelope,
    _validate_manifest,
)

STATES = tuple(sorted(OUTCOMES | {"running", "terminal_record_missing", "owner_state_unknown"}))
_TERMINAL = {"end", "block", "cancel"}


class ScanLimit(Exception):
    """The requested inventory bounds were reached."""


class BusyRun(Exception):
    """A writer holds the journal; inventory must not wait or interrupt it."""


def _read_run(directory: Path, *, max_events: int, deadline: float, now_ns: int) -> dict[str, Any]:
    _private_dir(directory)
    run_id = _id(directory.name)
    descriptor = os.open(directory / ".journal.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("invalid_journal_lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BusyRun from exc
        manifest = _read(directory / "run.json")
        _validate_manifest(manifest, run_id)
        _private_dir(directory / "events")
        _private_dir(directory / "delivery")
        paths = []
        with os.scandir(directory / "events") as entries:
            for entry in entries:
                if len(paths) >= max_events or time.monotonic() >= deadline:
                    raise ScanLimit
                if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                    raise ValueError("invalid_event_entry")
                paths.append(Path(entry.path))
        if not paths:
            raise ValueError("missing_start_record")
        events = []
        pending = held = accepted = 0
        pending_since = []
        for index, path in enumerate(sorted(paths)):
            if time.monotonic() >= deadline:
                raise ScanLimit
            event = _validate_envelope(
                _read(path), manifest, path.name, index, events[-1] if events else None
            )
            event_id = _id(event["event_id"])
            try:
                delivery = _read(directory / "delivery" / f"{event_id}.json")
            except FileNotFoundError:
                delivery = {}
            for signal in ("traces", "logs"):
                item = delivery.get(signal, {})
                if not isinstance(item, dict) or item.get("state", "pending") not in {
                    "pending",
                    "held",
                    "accepted",
                }:
                    raise ValueError("invalid_delivery_state")
                disposition = item.get("state", "pending")
                pending += disposition == "pending"
                held += disposition == "held"
                accepted += disposition == "accepted"
                if disposition == "pending":
                    pending_since.append(event["timestamp_ns"])
            events.append(event)
    finally:
        os.close(descriptor)

    last = events[-1]
    terminal = last["lifecycle"] in _TERMINAL
    if terminal and last["outcome"] not in OUTCOMES:
        raise ValueError("terminal_outcome_missing")
    owner_alive, identity = _process_identity(manifest["owner_pid"])
    expected = manifest.get("owner_start_identity")
    if owner_alive and (not expected or not identity):
        owner_alive = None
    elif owner_alive and expected != identity:
        owner_alive = False
    state = (
        last["outcome"]
        if terminal
        else "running"
        if owner_alive
        else "terminal_record_missing"
        if owner_alive is False
        else "owner_state_unknown"
    )
    return {
        "run_id": run_id,
        "operation": manifest["operation"],
        "state": state,
        "terminal": terminal,
        "owner_alive": owner_alive,
        "event_count": len(events),
        "last_lifecycle": last["lifecycle"],
        "reason_code": last.get("reason_code"),
        "pending_signals": pending,
        "held_signals": held,
        "accepted_signals": accepted,
        "pending_age_seconds": max(0, (now_ns - min(pending_since)) / 1e9) if pending_since else 0,
        "record_age_seconds": max(0, (now_ns - last["timestamp_ns"]) / 1e9),
        "missing_terminal_owner_dead": not terminal and owner_alive is False,
        "child_not_reaped": last.get("reason_code") == "child_not_reaped",
        "recovery_unverified": last.get("reason_code")
        in {"recovery_unverified", "recovery_required"},
    }


def snapshot(
    *, limit: int = 100, max_events: int = 200, max_seconds: float = 2.0
) -> dict[str, Any]:
    """Inspect at most limit root entries and max_events records in each run.

    Limits bound enumeration and per-file reads. The time budget is checked
    between reads; one current bounded file/process-identity read may finish
    after it. No blocking journal lock is acquired. Inventory order is not a
    recency promise; callers must respect truncated/incomplete before drawing
    estate-wide conclusions. The snapshot exposes opaque IDs, never paths/PIDs.
    """
    if (
        type(limit) is not int
        or not 1 <= limit <= 1000
        or type(max_events) is not int
        or not 1 <= max_events <= 1000
        or type(max_seconds) not in {int, float}
        or not 0.05 <= max_seconds <= 10
    ):
        raise ValueError("invalid_snapshot_bounds")
    started = time.monotonic()
    now_ns = time.time_ns()
    deadline = started + max_seconds
    rows = []
    errors: set[str] = set()
    unreadable = 0
    truncated = False
    root_present: bool | None = None
    visited = 0
    try:
        root = _root()
        root_present = True
        with os.scandir(root) as entries:
            for entry in entries:
                if visited >= limit or time.monotonic() >= deadline:
                    truncated = True
                    break
                visited += 1
                try:
                    _id(entry.name)
                    if not entry.is_dir(follow_symlinks=False):
                        raise ValueError("invalid_run_entry")
                    rows.append(
                        _read_run(
                            Path(entry.path),
                            max_events=max_events,
                            deadline=deadline,
                            now_ns=now_ns,
                        )
                    )
                except ScanLimit:
                    truncated = True
                    errors.add("ScanLimit")
                except Exception as exc:  # noqa: BLE001 - no path or exception message output
                    unreadable += 1
                    errors.add(type(exc).__name__)
    except FileNotFoundError:
        root_present = False
        errors.add("SourceMissing")
    except Exception as exc:  # noqa: BLE001 - an unreadable root is not empty/healthy
        unreadable += 1
        errors.add(type(exc).__name__)
    complete = root_present is True and not truncated and not unreadable
    counts = {state: sum(row["state"] == state for row in rows) for state in STATES}
    totals = {
        field: sum(row[field] for row in rows)
        for field in (
            "pending_signals",
            "held_signals",
            "accepted_signals",
            "missing_terminal_owner_dead",
            "child_not_reaped",
            "recovery_unverified",
        )
    }
    return {
        "schema_version": "agent_operations_snapshot.v1",
        "generated_at_ns": now_ns,
        "status": "complete" if complete else "incomplete",
        "complete": complete,
        "root_present": root_present,
        "truncated": truncated,
        "entries_visited": visited,
        "runs_scanned": len(rows),
        "unreadable_entries": unreadable,
        "error_types": sorted(errors),
        "counts": counts,
        **totals,
        "oldest_pending_age_seconds": max((row["pending_age_seconds"] for row in rows), default=0),
        "scan_duration_seconds": time.monotonic() - started,
        "runs": sorted(rows, key=lambda row: row["run_id"]),
    }


def metrics_text(value: dict[str, Any]) -> str:
    """Prometheus text format with only fixed metric names/state label values."""
    prefix = "token_burn_"
    metrics = (
        (
            "scan_complete",
            "Whether the bounded journal inventory was complete.",
            int(value["complete"]),
        ),
        (
            "scan_truncated",
            "Whether inventory exceeded an explicit scan bound.",
            int(value["truncated"]),
        ),
        (
            "unreadable_entries",
            "Journal entries that could not be inspected.",
            value["unreadable_entries"],
        ),
        ("runs_scanned", "Run journals read in the current inventory.", value["runs_scanned"]),
        (
            "pending_signals",
            "Observed signals awaiting collector acceptance.",
            value["pending_signals"],
        ),
        ("held_signals", "Observed signals held after collector rejection.", value["held_signals"]),
        (
            "oldest_pending_age_seconds",
            "Oldest observed pending signal age.",
            value["oldest_pending_age_seconds"],
        ),
        (
            "missing_terminal_owner_dead",
            "Observed dead-owner runs without a terminal record.",
            value["missing_terminal_owner_dead"],
        ),
        (
            "child_not_reaped",
            "Observed runs explicitly missing durable child-reaped proof.",
            value["child_not_reaped"],
        ),
        (
            "recovery_unverified",
            "Observed runs explicitly recording unverified recovery.",
            value["recovery_unverified"],
        ),
        (
            "snapshot_timestamp_seconds",
            "Wall-clock time of the source journal snapshot.",
            value["generated_at_ns"] / 1e9,
        ),
    )
    lines = []
    for name, help_text, number in metrics:
        lines.extend(
            [
                f"# HELP {prefix}{name} {help_text}",
                f"# TYPE {prefix}{name} gauge",
                f"{prefix}{name} {number}",
            ]
        )
    lines.extend(
        [
            f"# HELP {prefix}runs Observed runs by fixed lifecycle state.",
            f"# TYPE {prefix}runs gauge",
        ]
    )
    for state in STATES:
        lines.append(f'{prefix}runs{{state="{state}"}} {value["counts"][state]}')
    return "\n".join(lines) + "\n"
