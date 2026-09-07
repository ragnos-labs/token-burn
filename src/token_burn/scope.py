# SPDX-License-Identifier: Apache-2.0
"""One private operation scope across nested native cleanup calls.

Only initial journal persistence is a pre-effect requirement. Later telemetry
failures are reported alongside the unchanged native result, including when a
target has already been removed. No telemetry call grants cleanup authority.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import subprocess
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator

from token_burn.operations import REASON_CODES, Operation, _json, _write

_CURRENT: ContextVar[OperationScope | None] = ContextVar("agent_operation_scope", default=None)


def _source_revision() -> str | None:
    """Only attest the owning code checkout when it is currently clean."""
    root = Path(__file__).resolve().parents[2]
    if not (root / "src" / "token_burn" / "scope.py").is_file():
        return None
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            timeout=2,
            check=False,
        )
        if dirty.returncode or dirty.stdout:
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        value = head.stdout.strip() if head.returncode == 0 else ""
        return (
            value
            if len(value) in {40, 64} and all(c in "0123456789abcdef" for c in value)
            else None
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _classification(result: dict[str, Any]) -> tuple[str, str]:
    inner = result.get("remove") if isinstance(result.get("remove"), dict) else result
    reason = str(inner.get("reason_code") or "")
    if result.get("cancelled"):
        return "cancelled", "interrupted"
    if result.get("not_found"):
        return "completed", "already_absent"
    if result.get("ok") is True or result.get("execution_status") == "completed":
        return "completed", "completed"
    failed = bool(
        inner.get("failed_step") or inner.get("timed_out") or reason == "worktree_remove_failed"
    )
    blocked = result.get("execution_status") == "blocked" or (bool(reason) and not failed)
    outcome = "blocked" if blocked else "failed"
    if reason in REASON_CODES:
        return outcome, reason
    if reason in {"dirty_archive_failed", "recovery_artifact_missing"}:
        return outcome, "recovery_unverified"
    if reason in {
        "mutation_permit_target_mismatch",
        "mutation_permit_target_changed",
        "target_identity_mismatch",
        "identity_changed",
    }:
        return outcome, "target_identity_changed"
    if inner.get("timed_out"):
        return "failed", "timeout"
    if str(result.get("blocked_reason") or "").startswith("salvage_"):
        return "blocked", "recovery_unverified"
    if str(result.get("blocked_reason") or "").startswith("active:"):
        return "blocked", "active_owner_lease"
    return outcome, "admission_refused" if blocked else "internal_error"


class OperationScope:
    def __init__(self, name: str, source_ref: str | None) -> None:
        self.operation = Operation.start(
            name, source_revision=_source_revision(), source_ref=source_ref
        )
        self.errors: set[str] = set()
        self.closed = False
        self.delivery: dict[str, Any] = {"status": "pending", "collector_accepted": False}

    def export(self) -> None:
        try:
            self.delivery = self.operation.export_pending()
        except Exception as exc:  # noqa: BLE001 - export cannot change cleanup results
            self.errors.add(type(exc).__name__)

    def stage(self, name: str) -> None:
        self.delivery = {"status": "pending", "collector_accepted": False}
        try:
            self.operation.stage(name)
        except Exception as exc:  # noqa: BLE001 - preserve a native partial-effect result
            self.errors.add(type(exc).__name__)

    def complete(
        self,
        result: dict[str, Any],
        *,
        outcome: str | None = None,
        reason_code: str | None = None,
        export: bool = True,
    ) -> dict[str, Any]:
        if self.closed:
            return self.attach(result)
        self.closed = True
        chosen_outcome, chosen_reason = _classification(result)
        outcome = outcome or chosen_outcome
        reason_code = reason_code or chosen_reason
        receipt = None
        result_receipt = None
        digest = None
        try:
            path = self.operation.directory / f"result-{uuid.uuid4().hex}.json"
            _write(path, result)
            result_receipt = str(path)
            digest = "sha256:" + hashlib.sha256(_json(result)).hexdigest()
        except Exception as exc:  # noqa: BLE001 - target may already be gone
            self.errors.add(type(exc).__name__)
        try:
            receipt = self.operation.finish(outcome, reason_code=reason_code, evidence_ref=digest)
        except Exception as exc:  # noqa: BLE001 - expose missing terminal, never rewrite native outcome
            self.errors.add(type(exc).__name__)
        if export:
            self.export()
        attached = self.attach(result)
        attached["operation"].update(
            {
                "terminal_recorded": receipt is not None,
                "receipt": receipt["receipt"]
                if receipt
                else str(self.operation.directory / "run.json"),
                "result_receipt": result_receipt,
                "telemetry_pending": not self.delivery.get("collector_accepted", False)
                or bool(self.errors),
            }
        )
        return attached

    def attach(self, result: dict[str, Any]) -> dict[str, Any]:
        errors = self.errors | set(self.delivery.get("error_types", []))
        if self.delivery.get("error_type"):
            errors.add(self.delivery["error_type"])
        return {
            **result,
            "operation": {
                **self.operation.correlation(),
                "delivery_status": self.delivery.get("status", "pending"),
                "telemetry_pending": not self.delivery.get("collector_accepted", False)
                or bool(self.errors),
                "backend_verified": False,
                "error_types": sorted(errors),
            },
        }


@contextmanager
def operation_scope(name: str, *, source_ref: str | None = None) -> Iterator[OperationScope]:
    current = _CURRENT.get()
    if current is not None:
        yield current
        return
    scope = OperationScope(name, source_ref)
    token = _CURRENT.set(scope)
    try:
        scope.export()
        yield scope
    except BaseException as exc:
        cancelled = isinstance(exc, KeyboardInterrupt)
        blocked = isinstance(exc, (ValueError, SystemExit)) or hasattr(exc, "reason_code")
        outcome = "cancelled" if cancelled else "blocked" if blocked else "failed"
        reason = (
            "interrupted" if cancelled else "validation_failed" if blocked else "internal_error"
        )
        code = getattr(exc, "reason_code", None)
        if not cancelled and isinstance(code, str):
            if code in REASON_CODES:
                reason = code
            elif code.startswith(("archive_", "restored_", "restore_", "dirty_archive_")):
                reason = "recovery_unverified"
            elif code in {"active_process", "lease_owner_changed"}:
                reason = "active_owner_lease"
        record = scope.complete(
            {"ok": False, "error_type": type(exc).__name__},
            outcome=outcome,
            reason_code=reason,
            export=not cancelled,
        )
        try:
            exc.operation = record["operation"]
        except (AttributeError, TypeError):
            pass
        raise
    finally:
        _CURRENT.reset(token)


def record_stage(name: str) -> None:
    """Append only locally; never introduce network I/O at a mutation seam."""
    scope = _CURRENT.get()
    if scope is not None:
        scope.stage(name)


def observed(name: str, *, source_parameter: str = "worktree_path") -> Callable:
    """Observe an actual dictionary-returning native action, reusing its owner."""

    def decorate(function: Callable) -> Callable:
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            current = _CURRENT.get()
            if current is not None:
                return current.attach(function(*args, **kwargs))
            bound = signature.bind(*args, **kwargs)
            reference = bound.arguments.get(source_parameter)
            with operation_scope(
                name, source_ref=str(reference) if reference is not None else None
            ) as scope:
                return scope.complete(function(*args, **kwargs))

        return wrapped

    return decorate
