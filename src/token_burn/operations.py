# SPDX-License-Identifier: Apache-2.0
"""Private, durable CLI operation history with bounded, explicit OTLP replay.

This is evidence, never cleanup authority. Calls persist immutable
events and OTLP payloads before returning; only export_pending performs network
I/O. Acknowledgments mean collector acceptance, never backend verification.
Replay is at least once: an interrupted acknowledgment can resend identical
span IDs and log event IDs. Consumers must deduplicate log event_id values.
Partial success and permanent HTTP rejection remain held for inspection,
without automatic retry (https://opentelemetry.io/docs/specs/otlp/).
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from token_burn.config import otel_export_disabled

SERVICE_NAME = "token-burn"
SCHEMA_VERSION = "agent_operations.v1"
OPERATIONS = frozenset(
    {
        "evidence.capture",
        "evidence.cases",
        "evidence.read",
        "process.stop",
        "cleanup.admission",
        "cleanup.permit",
        "cleanup.remove",
        "worktree.cleanup",
        "worktree.archive",
        "worktree.restore",
    }
)
STAGES = frozenset(
    {
        "validate",
        "spawn",
        "running",
        "signal",
        "drain",
        "hash",
        "receipt",
        "admission",
        "recovery",
        "recheck",
        "remove",
        "branch_cleanup",
        "persist",
    }
)
OUTCOMES = frozenset({"completed", "failed", "blocked", "cancelled", "process_stopped"})
REASON_CODES = frozenset(
    {
        "completed",
        "process_exit",
        "process_signal",
        "timeout",
        "interrupted",
        "validation_failed",
        "permission_denied",
        "not_authorized",
        "admission_refused",
        "active_owner_lease",
        "lease_state_unknown",
        "target_identity_changed",
        "deferred",
        "internal_error",
        "export_unavailable",
        "already_absent",
        "unsafe_target",
        "dirty_worktree",
        "unmerged_branch",
        "recovery_required",
        "child_not_reaped",
        "recovery_unverified",
        "telemetry_stalled",
    }
)
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_SIGNALS = ("traces", "logs")
_MAX_FILE_BYTES = 128 * 1024
_MAX_EVENTS = 10000


def _json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _id(value: str) -> str:
    if not isinstance(value, str) or not _HEX32.fullmatch(value) or int(value, 16) == 0:
        raise ValueError("invalid_run_id")
    return value


def _choice(value: str, allowed: frozenset[str], code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(code)
    return value


def _private_dir(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("journal_directory_not_private")
    finally:
        os.close(fd)


def _root(*, create: bool = False) -> Path:
    path = (
        Path(
            os.getenv("TOKEN_BURN_STATE_DIR")
            or (Path.home() / ".local" / "state" / "token-burn" / "operations")
        )
        .expanduser()
        .absolute()
    )
    _private_dir(path, create=create)
    return path


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _process_identity(pid: int) -> tuple[bool | None, str | None]:
    """Read a local process birth identity. Never export its contents."""
    try:
        if sys.platform.startswith("linux"):
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] in {"Z", "X"}:
                return False, None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return True, boot + ":" + fields[19]
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=0.5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                state, identity = result.stdout.strip().split(None, 1)
                return (False, None) if state.startswith("Z") else (True, identity)
            return (False, None) if result.returncode == 1 else (None, None)
        os.kill(pid, 0)
        return None, None  # Liveness without birth identity cannot prove ownership.
    except (FileNotFoundError, ProcessLookupError):
        return False, None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None, None


def _read(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("journal_file_not_private")
        raw = stream.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("journal_file_too_large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid_journal_record")
    return value


def _write(path: Path, value: dict[str, Any], *, replace: bool = False) -> None:
    """Publish a complete fsynced file, then fsync its directory."""
    data = _json(value)
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("journal_file_too_large")
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            # link gives no-overwrite publication; all subsequent reads require
            # one link, after the temporary link has been removed below.
            os.link(temp, path, follow_symlinks=False)
            temp.unlink()
        _sync_dir(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def _lock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("journal_lock_not_private")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _attributes(event: dict[str, Any]) -> list[dict[str, Any]]:
    # This projection is intentionally explicit. Local source_ref never enters
    # the event, attributes, resource, span names, or log body.
    allowed = (
        "schema_version",
        "event_id",
        "run_id",
        "operation",
        "lifecycle",
        "stage",
        "outcome",
        "reason_code",
        "source_revision",
        "evidence_ref",
        "exit_code",
        "signal_number",
        "sequence",
    )
    attrs = []
    for key in allowed:
        value = event.get(key)
        if value is not None:
            attrs.append(
                {
                    "key": key,
                    "value": {"intValue": str(value)}
                    if type(value) is int
                    else {"stringValue": value},
                }
            )
    return attrs


def _payloads(event: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    attrs = _attributes(event)
    timestamp = str(event["timestamp_ns"])
    span = {
        "traceId": manifest["trace_id"],
        "spanId": event["span_id"],
        "parentSpanId": manifest["root_span_id"],
        "kind": 1,
        "name": "agent.operation." + event["lifecycle"],
        "startTimeUnixNano": timestamp,
        "endTimeUnixNano": timestamp,
        "attributes": attrs,
    }
    terminal = event["lifecycle"] in {"end", "block", "cancel"}
    error = event.get("outcome") == "failed"
    if terminal:
        span["status"] = {"code": 2 if error else 1}
    spans = [span]
    if terminal:
        spans.append(
            {
                "traceId": manifest["trace_id"],
                "spanId": manifest["root_span_id"],
                "kind": 1,
                "name": "agent.operation." + manifest["operation"],
                "startTimeUnixNano": str(manifest["started_ns"]),
                "endTimeUnixNano": timestamp,
                "attributes": attrs,
                "status": {"code": 2 if error else 1},
            }
        )
    resource = {"attributes": [{"key": "service.name", "value": {"stringValue": SERVICE_NAME}}]}
    scope = {"name": "token_burn.operations", "version": "1"}
    return {
        "traces": {
            "resourceSpans": [
                {"resource": resource, "scopeSpans": [{"scope": scope, "spans": spans}]}
            ]
        },
        "logs": {
            "resourceLogs": [
                {
                    "resource": resource,
                    "scopeLogs": [
                        {
                            "scope": scope,
                            "logRecords": [
                                {
                                    "timeUnixNano": timestamp,
                                    "observedTimeUnixNano": timestamp,
                                    "traceId": manifest["trace_id"],
                                    "spanId": event["span_id"],
                                    "severityNumber": 17 if error else 9,
                                    "severityText": "ERROR" if error else "INFO",
                                    "body": {"stringValue": _json(event).decode("ascii").strip()},
                                    "attributes": attrs,
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    }


def _hex_value(value: Any, width: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == width
        and all(character in "0123456789abcdef" for character in value)
        and any(character != "0" for character in value)
    )


def _validate_manifest(manifest: dict[str, Any], run_id: str) -> None:
    required = {
        "schema_version",
        "run_id",
        "trace_id",
        "root_span_id",
        "operation",
        "source_revision",
        "source_ref",
        "started_ns",
        "owner_pid",
        "owner_start_identity",
    }
    if (
        not required <= manifest.keys()
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("run_id") != run_id
        or not _hex_value(run_id, 32)
        or not _hex_value(manifest.get("trace_id"), 32)
        or not _hex_value(manifest.get("root_span_id"), 16)
        or type(manifest.get("started_ns")) is not int
        or not 0 < manifest["started_ns"] < 2**64
        or type(manifest.get("owner_pid")) is not int
        or not 0 < manifest["owner_pid"] < 2**31
    ):
        raise ValueError("invalid_operation_manifest")
    _choice(manifest["operation"], OPERATIONS, "invalid_operation_manifest")
    revision = manifest["source_revision"]
    if revision is not None and (
        not isinstance(revision, str) or not _REVISION.fullmatch(revision)
    ):
        raise ValueError("invalid_operation_manifest")
    for name in ("source_ref", "owner_start_identity"):
        value = manifest[name]
        if value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > 4096):
            raise ValueError("invalid_operation_manifest")


def _validate_envelope(
    envelope: dict[str, Any],
    manifest: dict[str, Any],
    filename: str,
    sequence: int,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """One integrity contract for local status, inventory and wire replay."""
    event = envelope.get("event")
    keys = {
        "schema_version",
        "event_id",
        "run_id",
        "trace_id",
        "span_id",
        "sequence",
        "timestamp_ns",
        "operation",
        "lifecycle",
        "stage",
        "outcome",
        "reason_code",
        "source_revision",
        "evidence_ref",
        "exit_code",
        "signal_number",
    }
    if not isinstance(event, dict) or event.keys() != keys:
        raise ValueError("invalid_operation_event")
    if (
        event["schema_version"] != SCHEMA_VERSION
        or event["run_id"] != manifest["run_id"]
        or event["trace_id"] != manifest["trace_id"]
        or event["operation"] != manifest["operation"]
        or event["source_revision"] != manifest["source_revision"]
        or not _hex_value(event["event_id"], 32)
        or not _hex_value(event["span_id"], 16)
        or event["span_id"] == manifest["root_span_id"]
        or type(event["sequence"]) is not int
        or event["sequence"] != sequence
        or filename != f"{sequence:06d}-{event['event_id']}.json"
        or type(event["timestamp_ns"]) is not int
        or not manifest["started_ns"] <= event["timestamp_ns"] < 2**64
        or (previous is not None and event["timestamp_ns"] < previous["timestamp_ns"])
    ):
        raise ValueError("invalid_operation_event")
    lifecycle = event["lifecycle"]
    _choice(lifecycle, {"start", "stage", "end", "block", "cancel"}, "invalid_operation_event")
    if (sequence == 0) != (lifecycle == "start") or (
        previous is not None and previous["lifecycle"] in {"end", "block", "cancel"}
    ):
        raise ValueError("invalid_operation_lifecycle")
    if event["reason_code"] is not None:
        _choice(event["reason_code"], REASON_CODES, "invalid_operation_event")
    terminal = lifecycle in {"end", "block", "cancel"}
    if terminal:
        _choice(event["outcome"], OUTCOMES, "invalid_operation_event")
        if (
            event["stage"] is not None
            or {"blocked": "block", "cancelled": "cancel"}.get(event["outcome"], "end") != lifecycle
        ):
            raise ValueError("invalid_operation_lifecycle")
    elif event["outcome"] is not None or any(
        event[name] is not None for name in ("evidence_ref", "exit_code", "signal_number")
    ):
        raise ValueError("invalid_operation_lifecycle")
    if lifecycle == "stage":
        _choice(event["stage"], STAGES, "invalid_operation_event")
    elif event["stage"] is not None or (lifecycle == "start" and event["reason_code"] is not None):
        raise ValueError("invalid_operation_lifecycle")
    if event["evidence_ref"] is not None and (
        not isinstance(event["evidence_ref"], str) or not _DIGEST.fullmatch(event["evidence_ref"])
    ):
        raise ValueError("invalid_operation_event")
    for name, lower, upper in (("exit_code", -255, 255), ("signal_number", 1, 127)):
        if event[name] is not None and (
            type(event[name]) is not int or not lower <= event[name] <= upper
        ):
            raise ValueError("invalid_operation_event")
    payloads = _payloads(event, manifest)
    if envelope.get("payloads") != payloads or envelope.get("payload_sha256") != {
        signal: hashlib.sha256(_json(payloads[signal])).hexdigest() for signal in _SIGNALS
    }:
        raise ValueError("operation_payload_integrity_mismatch")
    return event


class Operation:
    """One durable operational run. No method grants authority to act."""

    def __init__(self, directory: Path) -> None:
        _private_dir(directory)
        self.directory = directory
        self._manifest = _read(directory / "run.json")
        _validate_manifest(self._manifest, directory.name)

    @classmethod
    def start(
        cls, name: str, *, source_revision: str | None = None, source_ref: str | None = None
    ) -> Operation:
        _choice(name, OPERATIONS, "invalid_operation")
        if source_revision is not None and (
            not isinstance(source_revision, str) or not _REVISION.fullmatch(source_revision)
        ):
            raise ValueError("invalid_source_revision")
        if source_ref is not None and (
            not isinstance(source_ref, str) or len(source_ref.encode("utf-8")) > 4096
        ):
            raise ValueError("invalid_local_source_ref")
        root = _root(create=True)
        run_id = uuid.uuid4().hex
        directory = root / run_id
        directory.mkdir(mode=0o700)
        for subdir in ("events", "delivery"):
            (directory / subdir).mkdir(mode=0o700)
        _write(
            directory / "run.json",
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "trace_id": uuid.uuid4().hex,
                "root_span_id": uuid.uuid4().hex[:16],
                "operation": name,
                "source_revision": source_revision,
                "source_ref": source_ref,
                "started_ns": time.time_ns(),
                "owner_pid": os.getpid(),
                "owner_start_identity": _process_identity(os.getpid())[1],
            },
        )
        _sync_dir(directory)
        _sync_dir(root)
        operation = cls(directory)
        operation._record("start")
        return operation

    @classmethod
    def open(cls, run_id: str) -> Operation:
        return cls(_root() / _id(run_id))

    def correlation(self) -> dict[str, str]:
        return {
            "run_id": self._manifest["run_id"],
            "trace_id": self._manifest["trace_id"],
            "root_span_id": self._manifest["root_span_id"],
            "service_name": SERVICE_NAME,
        }

    def _events(self) -> list[dict[str, Any]]:
        _private_dir(self.directory / "events")
        paths = []
        with os.scandir(self.directory / "events") as entries:
            for entry in entries:
                if len(paths) >= _MAX_EVENTS:
                    raise ValueError("too_many_operation_events")
                if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                    raise ValueError("invalid_operation_event_entry")
                paths.append(Path(entry.path))
        envelopes = []
        previous = None
        for index, path in enumerate(sorted(paths)):
            envelope = _read(path)
            previous = _validate_envelope(envelope, self._manifest, path.name, index, previous)
            envelopes.append(envelope)
        return envelopes

    def _record(
        self,
        lifecycle: str,
        *,
        stage: str | None = None,
        outcome: str | None = None,
        reason_code: str | None = None,
        evidence_ref: str | None = None,
        exit_code: int | None = None,
        signal_number: int | None = None,
    ) -> dict[str, Any]:
        with _lock(self.directory / ".journal.lock"):
            envelopes = self._events()
            if any(e["event"]["lifecycle"] in {"end", "block", "cancel"} for e in envelopes):
                raise ValueError("operation_already_terminal")
            if len(envelopes) >= _MAX_EVENTS:
                raise ValueError("too_many_operation_events")
            event = {
                "schema_version": SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex,
                "run_id": self._manifest["run_id"],
                "trace_id": self._manifest["trace_id"],
                "span_id": uuid.uuid4().hex[:16],
                "sequence": len(envelopes),
                "timestamp_ns": max(
                    time.time_ns(),
                    self._manifest["started_ns"],
                    envelopes[-1]["event"]["timestamp_ns"] if envelopes else 0,
                ),
                "operation": self._manifest["operation"],
                "lifecycle": lifecycle,
                "stage": stage,
                "outcome": outcome,
                "reason_code": reason_code,
                "source_revision": self._manifest["source_revision"],
                "evidence_ref": evidence_ref,
                "exit_code": exit_code,
                "signal_number": signal_number,
            }
            payloads = _payloads(event, self._manifest)
            envelope = {
                "event": event,
                "payloads": payloads,
                "payload_sha256": {
                    s: hashlib.sha256(_json(payloads[s])).hexdigest() for s in _SIGNALS
                },
            }
            path = self.directory / "events" / f"{event['sequence']:06d}-{event['event_id']}.json"
            _write(path, envelope)
        return {
            **self.correlation(),
            "event_id": event["event_id"],
            "lifecycle": lifecycle,
            "outcome": outcome,
            "recorded": True,
            "collector_accepted": False,
            "backend_verified": False,
            "receipt": str(path),
        }

    def stage(self, name: str, *, reason_code: str | None = None) -> dict[str, Any]:
        _choice(name, STAGES, "invalid_stage")
        if reason_code is not None:
            _choice(reason_code, REASON_CODES, "invalid_reason_code")
        return self._record("stage", stage=name, reason_code=reason_code)

    def finish(
        self,
        outcome: str,
        *,
        reason_code: str | None = None,
        evidence_ref: str | None = None,
        exit_code: int | None = None,
        signal_number: int | None = None,
    ) -> dict[str, Any]:
        _choice(outcome, OUTCOMES, "invalid_outcome")
        if reason_code is not None:
            _choice(reason_code, REASON_CODES, "invalid_reason_code")
        if evidence_ref is not None and (
            not isinstance(evidence_ref, str) or not _DIGEST.fullmatch(evidence_ref)
        ):
            raise ValueError("invalid_evidence_digest")
        if exit_code is not None and (type(exit_code) is not int or not -255 <= exit_code <= 255):
            raise ValueError("invalid_exit_code")
        if signal_number is not None and (
            type(signal_number) is not int or not 1 <= signal_number <= 127
        ):
            raise ValueError("invalid_signal_number")
        lifecycle = {"blocked": "block", "cancelled": "cancel"}.get(outcome, "end")
        return self._record(
            lifecycle,
            outcome=outcome,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
            exit_code=exit_code,
            signal_number=signal_number,
        )

    def status(self) -> dict[str, Any]:
        with _lock(self.directory / ".journal.lock"):
            envelopes = self._events()
        pending = held = accepted = 0
        pending_since: list[int] = []
        errors: set[str] = set()
        for envelope in envelopes:
            state = self._delivery(envelope["event"]["event_id"])
            for signal in _SIGNALS:
                item = state.get(signal, {})
                disposition = item.get("state", "pending")
                accepted += disposition == "accepted"
                held += disposition == "held"
                pending += disposition not in {"accepted", "held"}
                if disposition not in {"accepted", "held"}:
                    pending_since.append(envelope["event"]["timestamp_ns"])
                if item.get("error_type"):
                    errors.add(item["error_type"])
        events = [envelope["event"] for envelope in envelopes]
        terminal = next(
            (e for e in reversed(events) if e["lifecycle"] in {"end", "block", "cancel"}), None
        )
        owner_alive, identity = _process_identity(self._manifest["owner_pid"])
        expected_identity = self._manifest["owner_start_identity"]
        if owner_alive and (not expected_identity or not identity):
            owner_alive = None
        elif owner_alive and identity != expected_identity:
            owner_alive = False
        state = (
            terminal["outcome"]
            if terminal
            else "running"
            if owner_alive
            else "terminal_record_missing"
            if owner_alive is False
            else "owner_state_unknown"
        )
        now = time.time_ns()
        return {
            **self.correlation(),
            "operation": self._manifest["operation"],
            "state": state,
            "running": terminal is None and owner_alive is True,
            "terminal": terminal is not None,
            "owner_alive": owner_alive,
            "owner_pid": self._manifest["owner_pid"],
            "pending_age_seconds": max(0, (now - min(pending_since)) / 1e9) if pending_since else 0,
            "record_age_seconds": max(0, (now - events[-1]["timestamp_ns"]) / 1e9)
            if events
            else None,
            "event_count": len(events),
            "pending_signals": pending,
            "held_signals": held,
            "accepted_signals": accepted,
            "collector_accepted": bool(events) and not pending and not held,
            "backend_verified": False,
            "missing_terminal": terminal is None,
            "missing_start": not any(e["lifecycle"] == "start" for e in events),
            "age_seconds": max(0, (time.time_ns() - self._manifest["started_ns"]) / 1e9),
            "last_event": events[-1] if events else None,
            "error_types": sorted(errors),
        }

    def read(self, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_read_bounds")
        with _lock(self.directory / ".journal.lock"):
            envelopes = self._events()
        rows = [e["event"] for e in envelopes[offset : offset + limit]]
        return {
            **self.correlation(),
            "events": rows,
            "next_offset": offset + len(rows),
            "has_more": offset + len(rows) < len(envelopes),
        }

    def _delivery(self, event_id: str) -> dict[str, Any]:
        path = self.directory / "delivery" / f"{_id(event_id)}.json"
        _private_dir(path.parent)
        try:
            return _read(path)
        except FileNotFoundError:
            return {}

    def export_pending(
        self, *, limit: int = 20, timeout: float = 0.25, max_seconds: float = 0.5
    ) -> dict[str, Any]:
        """Try at most limit HTTP requests. Fail-soft; never runs automatically."""
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(timeout) not in {int, float}
            or not 0.05 <= timeout <= 2.5
            or type(max_seconds) not in {int, float}
            or not 0.05 <= max_seconds <= 10
        ):
            raise ValueError("invalid_export_bounds")
        try:
            return self._export_pending(limit=limit, timeout=timeout, max_seconds=max_seconds)
        except Exception as exc:  # noqa: BLE001 - evidence survives export failures
            try:
                snapshot = self.status()
            except Exception:  # noqa: BLE001 - damaged journal must not hide the export failure
                snapshot = self.correlation()
            return {
                **snapshot,
                "status": "error",
                "error_type": type(exc).__name__,
                "collector_accepted": False,
                "backend_verified": False,
            }

    def _export_pending(self, *, limit: int, timeout: float, max_seconds: float) -> dict[str, Any]:
        if otel_export_disabled():
            return {**self.status(), "status": "disabled", "attempted_signals": 0}
        endpoint = _collector_endpoint()
        deadline = time.monotonic() + max_seconds
        attempted = 0
        with _lock(self.directory / ".export.lock", blocking=False) as acquired:
            if not acquired:
                return {**self.status(), "status": "busy", "attempted_signals": 0}
            with _lock(self.directory / ".journal.lock"):
                envelopes = self._events()
            for envelope in envelopes:
                event_id = envelope["event"]["event_id"]
                state = self._delivery(event_id)
                for signal in _SIGNALS:
                    if state.get(signal, {}).get("state") in {"accepted", "held"}:
                        continue
                    remaining = deadline - time.monotonic()
                    if attempted >= limit or remaining < 0.05:
                        break
                    payload = _json(envelope["payloads"][signal])
                    if hashlib.sha256(payload).hexdigest() != envelope["payload_sha256"][signal]:
                        raise ValueError("payload_digest_mismatch")
                    attempted += 1
                    result = _send(endpoint + "/v1/" + signal, payload, min(timeout, remaining))
                    result["attempts"] = state.get(signal, {}).get("attempts", 0) + 1
                    result["attempted_ns"] = time.time_ns()
                    state[signal] = result
                    _write(self.directory / "delivery" / f"{event_id}.json", state, replace=True)
                    if result["state"] != "accepted":
                        # A collector outage must not add one timeout per
                        # queued event to an inline CLI operation.
                        status = self.status()
                        return {**status, "status": "pending", "attempted_signals": attempted}
                if attempted >= limit or time.monotonic() >= deadline:
                    break
        status = self.status()
        return {
            **status,
            "status": "accepted" if status["collector_accepted"] else "pending",
            "attempted_signals": attempted,
        }


def _collector_endpoint() -> str:
    from token_burn.config import collector_endpoint

    return collector_endpoint()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("collector_deadline_exceeded")
    return remaining


class _DeadlineReader(io.RawIOBase):
    """Recompute remaining time for every socket read, including HTTP headers."""

    def __init__(self, stream: Any, connection: Any, deadline: float) -> None:
        self.stream, self.connection, self.deadline = stream, connection, deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int | None:
        self.connection.settimeout(_remaining(self.deadline))
        return self.stream.readinto(buffer)

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            super().close()


class _DeadlineSocket:
    """Keep standard HTTP parsing while bounding sends and buffered receives."""

    def __init__(self, connection: Any, deadline: float) -> None:
        self.connection, self.deadline = connection, deadline

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def sendall(self, data: bytes) -> None:
        self.connection.settimeout(_remaining(self.deadline))
        self.connection.sendall(data)

    def makefile(self, mode: str) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError("unsupported_collector_stream")
        raw = self.connection.makefile(mode, buffering=0)
        return io.BufferedReader(_DeadlineReader(raw, self.connection, self.deadline))


def _send(url: str, payload: bytes, timeout: float) -> dict[str, Any]:
    # http.client has no proxy or redirect machinery. Bound the complete
    # transport, not an inactivity timeout that a dribbling peer can renew.
    connection = None
    deadline = time.monotonic() + timeout
    try:
        route = urllib.parse.urlsplit(url)
        if (
            route.scheme != "http"
            or route.hostname not in {"127.0.0.1", "::1"}
            or route.username is not None
            or route.password is not None
            or route.query
            or route.fragment
        ):
            raise ValueError("collector_route_rejected")
        connection = http.client.HTTPConnection(
            route.hostname, route.port, timeout=_remaining(deadline)
        )
        connection.connect()
        connection.sock = _DeadlineSocket(connection.sock, deadline)
        connection.request(
            "POST",
            route.path,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        with connection.getresponse() as response:
            if response.status != 200:
                return {
                    "state": "pending" if response.status in {429, 502, 503, 504} else "held",
                    "error_type": "HTTPError",
                    "http_status": response.status,
                }
            body = response.read(65537)
            if len(body) > 65536:
                return {"state": "held", "error_type": "InvalidResponse"}
            if response.headers.get_content_type() != "application/json":
                return {"state": "held", "error_type": "InvalidContentType"}
            value = json.loads(body)
            if not isinstance(value, dict):
                return {"state": "held", "error_type": "InvalidResponse"}
            # Collector 0.143.1 emits an empty message for full acceptance.
            # Only this exact zero-field shape is normalized; populated
            # partial responses remain held and are never replayed blindly.
            if value == {"partialSuccess": {}}:
                return {"state": "accepted", "error_type": None}
            if "partialSuccess" in value:
                return {"state": "held", "error_type": "PartialSuccess"}
            if value:
                return {"state": "held", "error_type": "InvalidResponse"}
            return {"state": "accepted", "error_type": None}
    except Exception as exc:  # noqa: BLE001 - never store response/exception text
        return {"state": "pending", "error_type": type(exc).__name__}
    finally:
        if connection is not None:
            connection.close()
