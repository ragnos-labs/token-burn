# SPDX-License-Identifier: Apache-2.0
"""Bounded local handoffs that preserve protected state, not execution authority."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any, Mapping

from token_burn._local import ContractError, encoded

SCHEMA_VERSION = "token_burn.handoff.v1"
PREFERRED_BYTES = 4096
MAX_BYTES = 32768
PROTECTED_FIELDS = (
    "objective",
    "authority",
    "stop_conditions",
    "changed_files",
    "test_state",
    "receipts",
    "active_processes",
    "blockers",
    "next_action",
)
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


def _strings(value: Any, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ContractError(f"invalid_handoff_{field}")


def _correlation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"legacy", "operation"}:
        raise ContractError("invalid_handoff_correlation")
    if "legacy" in value:
        legacy = value["legacy"]
        if not isinstance(legacy, dict) or any(
            not isinstance(k, str)
            or not _OPAQUE.fullmatch(k)
            or not isinstance(v, str)
            or not _OPAQUE.fullmatch(v)
            for k, v in legacy.items()
        ):
            raise ContractError("invalid_legacy_correlation")
    if "operation" in value:
        operation = value["operation"]
        widths = {"run_id": 32, "trace_id": 32, "root_span_id": 16}
        if (
            not isinstance(operation, dict)
            or set(operation) != set(widths)
            or any(
                not isinstance(operation[k], str)
                or re.fullmatch(r"[0-9a-f]{" + str(width) + "}", operation[k]) is None
                for k, width in widths.items()
            )
        ):
            raise ContractError("invalid_operation_correlation")
    return value


def create(
    state: Mapping[str, Any], *, correlation: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Copy all protected state; reject unknown fields instead of silently losing it."""
    if not isinstance(state, Mapping) or set(state) != set(PROTECTED_FIELDS):
        raise ContractError("handoff_requires_exact_protected_fields")
    if correlation is not None and not isinstance(correlation, Mapping):
        raise ContractError("invalid_handoff_correlation")
    for field in ("objective", "next_action"):
        if not isinstance(state[field], str) or not state[field].strip():
            raise ContractError(f"invalid_handoff_{field}")
    for field in ("authority", "test_state"):
        if not isinstance(state[field], dict) or not state[field]:
            raise ContractError(f"invalid_handoff_{field}")
    for field in ("stop_conditions", "changed_files", "receipts", "blockers"):
        _strings(state[field], field)
    if not isinstance(state["active_processes"], list):
        raise ContractError("invalid_handoff_active_processes")
    for path in state["changed_files"]:
        if (
            not path
            or path.startswith(("/", "~", "\\"))
            or "\\" in path
            or ".." in PurePosixPath(path).parts
            or ":" in path
            or any(ord(char) < 32 for char in path)
        ):
            raise ContractError("handoff_paths_must_be_repository_relative")
    return seal(
        {
            "schema_version": SCHEMA_VERSION,
            "state": dict(state),
            "correlation": _correlation(dict(correlation or {})),
        }
    )


def seal(
    packet: Mapping[str, Any],
    *,
    max_bytes: int = MAX_BYTES,
    preferred_bytes: int = PREFERRED_BYTES,
    inclusive_preferred: bool = False,
    trailing_newline: bool = True,
) -> dict[str, Any]:
    """Bound and snapshot JSON without changing an owner's envelope or dropping fields.

    Only ``bytes`` and ``preferred_target_exceeded`` are computed. This helper
    validates JSON and size, not state, privacy, correlation, or action authority.
    The preferred flag is conservative at its one-byte true/false width boundary.
    """
    if (
        not isinstance(packet, Mapping)
        or type(max_bytes) is not int
        or max_bytes < 1
        or type(preferred_bytes) is not int
        or preferred_bytes < 0
        or type(inclusive_preferred) is not bool
        or type(trailing_newline) is not bool
    ):
        raise ContractError("invalid_handoff_size_policy")
    limit = min(max_bytes, MAX_BYTES)
    result = json.loads(encoded(dict(packet)))
    result["bytes"] = 0
    result["preferred_target_exceeded"] = False

    def size() -> int:
        return len(encoded(result)) - int(not trailing_newline)

    def converge() -> None:
        # Integer width can grow only a few times before the 32 KiB ceiling.
        for _ in range(8):
            measured = size()
            if result["bytes"] == measured:
                return
            result["bytes"] = measured
        raise ContractError("handoff_size_did_not_converge")

    converge()
    result["preferred_target_exceeded"] = (
        result["bytes"] >= preferred_bytes
        if inclusive_preferred
        else result["bytes"] > preferred_bytes
    )
    converge()
    if result["bytes"] > limit:
        raise ContractError("handoff_too_large")
    return result


def validate(packet: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(packet, Mapping)
        or set(packet)
        != {"schema_version", "state", "correlation", "bytes", "preferred_target_exceeded"}
        or packet.get("schema_version") != SCHEMA_VERSION
    ):
        raise ContractError("invalid_handoff_schema")
    rebuilt = create(packet["state"], correlation=packet["correlation"])
    if encoded(rebuilt) != encoded(packet):
        raise ContractError("handoff_integrity_mismatch")
    return rebuilt
