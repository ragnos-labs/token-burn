# SPDX-License-Identifier: Apache-2.0
"""Explicit native usage collection. No discovery, model calls, or account access."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from token_burn._local import ContractError, digest, encoded, read_json, state_lock, write_json

SCHEMA_VERSION = "token_burn.usage.v1"
PARSER_VERSION = "2"
METRIC_VERSION = "processed_tokens.v1"
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_COLLECTION_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_ROWS = 100_000
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_SOURCES = 256
MAX_TASKS = 128
TOKEN_FIELDS = (
    "input_tokens",
    "fresh_input_tokens",
    "cache_creation_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "processed_tokens",
)
TASK_FIELDS = {
    "task_id",
    "repository_id",
    "client",
    "model",
    "reasoning_effort",
    "revision",
    "prompt_sha256",
    "validation_sha256",
    "round",
    "archetype",
    "sources_complete",
}
FORMATS = {
    "codex_exec_jsonl": "codex",
    "codex_rollout_jsonl": "codex",
    "claude_print_json": "claude_code",
    "claude_transcript_jsonl": "claude_code",
}
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,159}$")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]*(?:\[[1-9][0-9]*[km]\])?")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)?$")


def _label(value: Any) -> bool:
    return isinstance(value, str) and _LABEL.fullmatch(value) is not None


def _model(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 160 and _MODEL.fullmatch(value) is not None


def _repository(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 160 and _REPOSITORY.fullmatch(value) is not None


def _hex(value: Any, widths: tuple[int, ...] = (64,)) -> bool:
    return (
        isinstance(value, str)
        and len(value) in widths
        and re.fullmatch("[0-9a-f]+", value) is not None
    )


def validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "variant", "tasks", "sources"}
        or value.get("schema_version") != "token_burn.sources.v1"
        or value.get("variant") not in {"baseline", "candidate"}
    ):
        raise ContractError("invalid_sources_schema")
    tasks, sources = value["tasks"], value["sources"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ContractError("invalid_task_roster")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ContractError("invalid_source_roster")
    by_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if (
            not isinstance(task, dict)
            or set(task) != TASK_FIELDS
            or any(not _label(task[k]) for k in ("task_id", "reasoning_effort", "archetype"))
            or not _model(task["model"])
            or not _repository(task["repository_id"])
            or task["client"] not in {"codex", "claude_code"}
            or type(task["round"]) is not int
            or task["round"] < 1
            or not _hex(task["revision"], (40, 64))
            or any(not _hex(task[k]) for k in ("prompt_sha256", "validation_sha256"))
            or type(task["sources_complete"]) is not bool
            or task["task_id"] in by_id
        ):
            raise ContractError("invalid_task_contract")
        by_id[task["task_id"]] = task
    seen: set[str] = set()
    locations: set[tuple[str, int, int | None]] = set()
    for source in sources:
        required = {"source_id", "task_id", "path", "format", "kind"}
        optional = {"initial_usage", "start_line", "end_line", "account_ref"}
        if (
            not isinstance(source, dict)
            or not required <= set(source)
            or set(source) - required - optional
            or not _label(source.get("source_id"))
            or source["source_id"] in seen
            or source.get("task_id") not in by_id
            or source.get("format") not in FORMATS
            or source.get("kind") not in {"root", "child", "continuation"}
            or not isinstance(source.get("path"), str)
            or not Path(source["path"]).is_absolute()
            or not _label(source.get("account_ref", "default"))
        ):
            raise ContractError("invalid_source_contract")
        if FORMATS[source["format"]] != by_id[source["task_id"]]["client"]:
            raise ContractError("source_client_mismatch")
        start, end = source.get("start_line", 1), source.get("end_line")
        if (
            type(start) is not int
            or start < 1
            or (end is not None and (type(end) is not int or end < start))
        ):
            raise ContractError("invalid_source_range")
        if source["format"] == "claude_print_json" and (start != 1 or end is not None):
            raise ContractError("json_source_does_not_support_line_range")
        location = (str(Path(source["path"]).resolve()), start, end)
        if location in locations:
            raise ContractError("duplicate_source_range")
        if "initial_usage" in source:
            if source["format"] != "codex_rollout_jsonl":
                raise ContractError("initial_usage_requires_cumulative_source")
            _codex(source["initial_usage"])
        seen.add(source["source_id"])
        locations.add(location)
    return json.loads(encoded(value))


def _integer(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ContractError("invalid_usage_counter")
    return value


def _codex(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ContractError("missing_usage")
    input_tokens = _integer(value.get("input_tokens"))
    cached = _integer(value.get("cached_input_tokens", 0))
    creation = _integer(value.get("cache_write_input_tokens", 0))
    output = _integer(value.get("output_tokens"))
    reasoning = _integer(value.get("reasoning_output_tokens", 0))
    if cached + creation > input_tokens or reasoning > output:
        raise ContractError("inconsistent_usage_components")
    return {
        "input_tokens": input_tokens,
        "fresh_input_tokens": input_tokens - cached - creation,
        "cache_creation_tokens": creation,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "processed_tokens": input_tokens + output,
    }


def _claude(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ContractError("missing_usage")
    fresh = _integer(value.get("input_tokens"))
    creation = _integer(value.get("cache_creation_input_tokens", 0))
    cached = _integer(value.get("cache_read_input_tokens", 0))
    output = _integer(value.get("output_tokens"))
    reasoning = _integer(value.get("reasoning_output_tokens", 0))
    if reasoning > output:
        raise ContractError("inconsistent_usage_components")
    total_input = fresh + creation + cached
    return {
        "input_tokens": total_input,
        "fresh_input_tokens": fresh,
        "cache_creation_tokens": creation,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "processed_tokens": total_input + output,
    }


def normalize_codex_usage(value: Mapping[str, Any]) -> dict[str, int]:
    """Normalize one native sample, not its cumulative history; invalid usage raises."""
    return _codex(value)


def normalize_claude_usage(value: Mapping[str, Any]) -> dict[str, int]:
    """Normalize one Claude usage record, including separate cache input components."""
    return _claude(value)


def _stamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ContractError("missing_usage_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise ContractError("invalid_usage_timestamp") from exc


def _rows(path: Path, source: Mapping[str, Any]):
    start, end = source.get("start_line", 1), source.get("end_line")
    count = 0
    with path.open("rb") as handle:
        line_number = 0
        while line := handle.readline(MAX_LINE_BYTES + 1):
            line_number += 1
            if len(line) > MAX_LINE_BYTES:
                raise ContractError("source_line_too_large")
            if end is not None and line_number > end:
                break
            if not line.strip():
                continue
            count += 1
            if count > MAX_ROWS:
                raise ContractError("too_many_source_rows")
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError) as exc:
                if line_number < start:
                    continue
                raise ContractError("invalid_native_json") from exc
            if not isinstance(row, dict):
                if line_number < start:
                    continue
                raise ContractError("invalid_native_row")
            if line_number < start and not (
                source["format"].startswith("codex_")
                and row.get("type") in {"session_meta", "turn_context", "thread.started"}
            ):
                continue
            yield row
    if line_number < start or (end is not None and line_number < end):
        raise ContractError("source_range_incomplete")


def _identity(value: Any, account: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError("missing_native_identity")
    return digest([account, value])


def _parse(path: Path, source: Mapping[str, Any]) -> dict[str, Any]:
    format_name = source["format"]
    account = source.get("account_ref", "default")
    events: list[dict[str, Any]] = []
    models: set[str] = set()
    session = ""
    reasons: list[str] = []
    initial = _codex(source["initial_usage"]) if "initial_usage" in source else None
    forked = source["kind"] != "root"
    if format_name == "claude_print_json":
        rows = [read_json(path, max_bytes=MAX_SOURCE_BYTES)]
    else:
        rows = _rows(path, source)
    pending_turn = False
    previous: dict[str, int] | None = initial
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ContractError("invalid_native_row")
            if format_name == "codex_exec_jsonl":
                if row.get("type") == "thread.started":
                    found = _identity(row.get("thread_id"), account)
                    if session and found != session:
                        raise ContractError("multiple_native_sessions_in_source")
                    session = found
                elif row.get("type") == "turn.started":
                    pending_turn = True
                elif row.get("type") in {"turn.failed", "error"}:
                    reasons.append("native_run_failed")
                elif row.get("type") == "turn.completed":
                    if not session:
                        raise ContractError("missing_native_identity")
                    usage = _codex(row.get("usage"))
                    events.append(
                        {
                            "mode": "delta",
                            "session": session,
                            "key": digest([source["source_id"], len(events)]),
                            "usage": usage,
                        }
                    )
                    pending_turn = False
            elif format_name == "codex_rollout_jsonl":
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                if row.get("type") == "session_meta":
                    found = _identity(payload.get("id"), account)
                    if session and found != session:
                        raise ContractError("multiple_native_sessions_in_source")
                    session = found
                    forked = (
                        forked
                        or bool(payload.get("forked_from_id"))
                        or bool(payload.get("parent_thread_id"))
                        or payload.get("thread_source") == "subagent"
                    )
                    source_meta = payload.get("source")
                    forked = forked or isinstance(source_meta, dict) and "subagent" in source_meta
                elif row.get("type") == "turn_context" and "model" in payload:
                    if not _model(payload["model"]):
                        raise ContractError("native_model_invalid")
                    models.add(payload["model"])
                elif row.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    if info.get("total_token_usage") is None:
                        # Native status can have an empty usage observation before work begins.
                        continue
                    if not session:
                        raise ContractError("missing_native_identity")
                    usage = _codex(info["total_token_usage"])
                    timestamp = _stamp(row.get("timestamp"))
                    if not events and initial is not None:
                        if any(usage[k] < initial[k] for k in ("input_tokens", "output_tokens")):
                            raise ContractError("inherited_baseline_after_observation")
                        events.append(
                            {
                                "mode": "seed",
                                "session": session,
                                "stamp": timestamp,
                                "key": digest([session, timestamp, "seed", initial]),
                                "usage": initial,
                            }
                        )
                    # Cache/reasoning components can be reattributed without any
                    # new full-input/output spend. They must never reset the
                    # primary counters and replay an already-counted session.
                    primary_decreases = (
                        [usage[k] < previous[k] for k in ("input_tokens", "output_tokens")]
                        if previous is not None
                        else [False, False]
                    )
                    if any(primary_decreases) and not all(primary_decreases):
                        reasons.append("primary_counter_reset_ambiguous")
                    reset = all(primary_decreases)
                    events.append(
                        {
                            "mode": "cumulative",
                            "session": session,
                            "stamp": timestamp,
                            "reset": reset,
                            "key": digest([session, timestamp, usage]),
                            "usage": usage,
                        }
                    )
                    previous = usage
            elif format_name == "claude_transcript_jsonl":
                message = row.get("message") if isinstance(row.get("message"), dict) else {}
                if (message.get("role") or row.get("type")) != "assistant":
                    continue
                if "usage" not in message:
                    continue
                usage = _claude(message["usage"])
                message_id = _identity(message.get("id"), account)
                if "model" in message:
                    if not _model(message["model"]):
                        raise ContractError("native_model_invalid")
                    models.add(message["model"])
                events.append(
                    {
                        "mode": "message",
                        "session": digest([account, row.get("sessionId", source["source_id"])]),
                        "key": message_id,
                        "usage": usage,
                    }
                )
            else:
                if row.get("type") != "result" or row.get("is_error") is not False:
                    raise ContractError("native_run_failed")
                usage = _claude(row.get("usage"))
                model_usage = row.get("modelUsage")
                if (
                    not isinstance(model_usage, dict)
                    or not model_usage
                    or any(not _model(model) for model in model_usage)
                    or usage["processed_tokens"] <= 0
                ):
                    raise ContractError("native_model_usage_unavailable")
                models.update(model_usage)
                session = _identity(row.get("session_id"), account)
                events.append(
                    {
                        "mode": "delta",
                        "session": session,
                        "key": digest([source["source_id"], 0]),
                        "usage": usage,
                    }
                )
            if len(models) > 32:
                raise ContractError("too_many_native_models")
    except (ContractError, OSError) as exc:
        reasons.append(str(exc) if isinstance(exc, ContractError) else "source_unreadable")
    if pending_turn:
        reasons.append("native_turn_incomplete")
    if (
        format_name == "codex_rollout_jsonl"
        and (forked or source.get("start_line", 1) > 1)
        and initial is None
    ):
        reasons.append("inherited_usage_baseline_unavailable")
    if not any(event["mode"] != "seed" for event in events):
        reasons.append("native_usage_unavailable")
    elif not any(
        event["usage"]["processed_tokens"] > 0 for event in events if event["mode"] != "seed"
    ):
        reasons.append("native_usage_unavailable")
    return {
        "events": events,
        "observed_models": sorted(models)[:32],
        "reasons": sorted(set(reasons)),
        # Aggregate CLI logs lack stable per-turn event IDs. Same-session,
        # same-usage replay is ambiguous even when file formatting differs;
        # keep it unavailable instead of using a producer-selected source ID
        # to bill the same native work a second time.
        "aggregate_identity": digest([format_name, [(e["session"], e["usage"]) for e in events]])
        if format_name in {"codex_exec_jsonl", "claude_print_json"} and events
        else None,
    }


def _file_digest(path: Path) -> tuple[str, tuple[int, int, int, int, int]]:
    before = path.stat()
    if not path.is_file() or before.st_size > MAX_SOURCE_BYTES:
        raise ContractError("source_too_large_or_not_regular")
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise ContractError("source_too_large_or_not_regular")
            hasher.update(chunk)
    stamp = _fingerprint(before)
    if stamp != _fingerprint(path.stat()):
        raise ContractError("source_changed_during_collection")
    return hasher.hexdigest(), stamp


def _fingerprint(value: Any) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _zero() -> dict[str, int]:
    return dict.fromkeys(TOKEN_FIELDS, 0)


def _aggregate(
    entries: list[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[dict[str, int], list[str], int]:
    totals = _zero()
    reasons: list[str] = []
    events: list[dict[str, Any]] = []
    modes: dict[str, set[str]] = {}
    source_signatures: set[tuple[str, str]] = set()
    aggregate_event_sources: dict[str, str] = {}
    for source, parsed in entries:
        events.extend(parsed["events"])
        reasons.extend(parsed["reasons"])
        if source["format"] in {"codex_exec_jsonl", "claude_print_json"}:
            signature = (source["format"], parsed.get("aggregate_identity", ""))
            if signature in source_signatures:
                reasons.append("duplicate_native_source")
            source_signatures.add(signature)
        for event in parsed["events"]:
            modes.setdefault(event["session"], set()).add(source["format"])
            if event["mode"] == "delta":
                native_event = digest([source["format"], event["session"], event["usage"]])
                prior_source = aggregate_event_sources.setdefault(native_event, source["source_id"])
                if prior_source != source["source_id"]:
                    reasons.append("duplicate_native_source")
    if any(len(value) > 1 for value in modes.values()):
        reasons.append("overlapping_native_formats")
    high_water: dict[str, dict[str, int]] = {}
    seen: dict[str, dict[str, int]] = {}
    duplicates = 0
    for event in sorted(events, key=lambda e: (e.get("stamp", ""), e["mode"] != "seed")):
        usage = event["usage"]
        key = event["key"]
        if key in seen:
            duplicates += 1
            if usage != seen[key]:
                reasons.append("conflicting_duplicate_usage")
            continue
        seen[key] = usage
        if event["mode"] in {"seed", "cumulative"}:
            previous = high_water.setdefault(event["session"], _zero())
            if event.get("reset"):
                previous = _zero()
                high_water[event["session"]] = previous
            delta = {field: max(0, usage[field] - previous[field]) for field in TOKEN_FIELDS}
            for field in TOKEN_FIELDS:
                previous[field] = max(previous[field], usage[field])
            if event["mode"] == "seed":
                continue
            delta["processed_tokens"] = delta["input_tokens"] + delta["output_tokens"]
        else:
            delta = usage
        for field in TOKEN_FIELDS:
            totals[field] += delta[field]
    return totals, sorted(set(reasons)), duplicates


def collect(manifest: Mapping[str, Any], *, state_dir: Path) -> dict[str, Any]:
    """Collect only listed sources, serializing one private bounded cache writer."""
    manifest = validate_manifest(manifest)
    manifest_sha = digest(manifest)
    tasks = sorted(manifest["tasks"], key=lambda task: task["task_id"])
    roster_sha = digest(
        [{k: v for k, v in task.items() if k != "sources_complete"} for task in tasks]
    )
    task_entries: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {
        task["task_id"]: [] for task in tasks
    }
    source_reports: list[dict[str, Any]] = []
    hits = misses = 0
    with state_lock(state_dir) as directory:
        cache_path = directory / "usage-cache.json"
        try:
            cached = read_json(cache_path, max_bytes=MAX_CACHE_BYTES)
            if (
                not isinstance(cached, dict)
                or cached.get("schema_version") != "token_burn.cache.v2"
                or cached.get("parser_version") != PARSER_VERSION
                or cached.get("manifest_sha256") != manifest_sha
                or not isinstance(cached.get("entries"), dict)
            ):
                cached = {}
        except (OSError, ContractError):
            cached = {}
        cache_entries = cached.get("entries", {})
        new_entries: dict[str, Any] = {}
        collection_bytes = 0
        for source in manifest["sources"]:
            content_sha: str | None = None
            try:
                path = Path(source["path"])
                collection_bytes += path.stat().st_size
                if collection_bytes > MAX_COLLECTION_BYTES:
                    raise ContractError("collection_byte_limit_exceeded")
                content_sha, fingerprint = _file_digest(path)
                cache_key = digest([source, content_sha])
                entry = cache_entries.get(cache_key)
                if (
                    isinstance(entry, dict)
                    and set(entry) == {"payload", "sha256"}
                    and isinstance(entry["payload"], dict)
                    and digest(entry["payload"]) == entry["sha256"]
                    and isinstance(entry["payload"].get("events"), list)
                    and isinstance(entry["payload"].get("reasons"), list)
                    and isinstance(entry["payload"].get("observed_models"), list)
                ):
                    parsed = entry["payload"]
                    hits += 1
                else:
                    parsed = _parse(path, source)
                    misses += 1
                if fingerprint != _fingerprint(path.stat()):
                    raise ContractError("source_changed_during_collection")
                parsed = {**parsed, "content_sha256": content_sha}
                new_entries[cache_key] = {"payload": parsed, "sha256": digest(parsed)}
            except (ContractError, OSError) as exc:
                parsed = {
                    "events": [],
                    "observed_models": [],
                    "reasons": [
                        str(exc) if isinstance(exc, ContractError) else "source_unreadable"
                    ],
                    "content_sha256": content_sha,
                    "aggregate_identity": None,
                }
                misses += 1
            task_entries[source["task_id"]].append((source, parsed))
            source_reports.append(
                {
                    "source_id": source["source_id"],
                    "task_id": source["task_id"],
                    "format": source["format"],
                    "kind": source["kind"],
                    "content_sha256": content_sha,
                    "observed_models": parsed["observed_models"],
                    "coverage": {
                        "status": "unavailable" if parsed["reasons"] else "measured",
                        "reasons": parsed["reasons"],
                    },
                }
            )
        cache_payload = {
            "schema_version": "token_burn.cache.v2",
            "parser_version": PARSER_VERSION,
            "manifest_sha256": manifest_sha,
            "entries": new_entries,
        }
        evicted = 0
        while len(encoded(cache_payload)) > MAX_CACHE_BYTES and new_entries:
            del new_entries[next(iter(new_entries))]
            evicted += 1
        write_json(cache_path, cache_payload, replace=True)
    reports: list[dict[str, Any]] = []
    all_reasons: set[str] = set()
    observed_owners: dict[str, str] = {}
    ambiguous_tasks: set[str] = set()
    for task_id, entries in task_entries.items():
        for source, parsed in entries:
            if parsed.get("aggregate_identity"):
                owner = observed_owners.setdefault(parsed["aggregate_identity"], task_id)
                if owner != task_id:
                    ambiguous_tasks.update((owner, task_id))
            for event in parsed["events"]:
                # One native usage event cannot be attributed to two benchmark tasks.
                owner = observed_owners.setdefault(event["key"], task_id)
                if owner != task_id:
                    ambiguous_tasks.update((owner, task_id))
                native_source = digest(
                    [source["format"], parsed["content_sha256"], event["session"]]
                )
                owner = observed_owners.setdefault(native_source, task_id)
                if owner != task_id:
                    ambiguous_tasks.update((owner, task_id))
                if event["mode"] == "delta":
                    native_event = digest([source["format"], event["session"], event["usage"]])
                    owner = observed_owners.setdefault(native_event, task_id)
                    if owner != task_id:
                        ambiguous_tasks.update((owner, task_id))
    for task in tasks:
        entries = task_entries[task["task_id"]]
        totals, reasons, duplicates = _aggregate(entries)
        if not entries:
            reasons.append("task_sources_unavailable")
        if not task["sources_complete"]:
            reasons.append("task_source_scope_incomplete")
        if task["task_id"] in ambiguous_tasks:
            reasons.append("ambiguous_task_attribution")
        models = sorted({model for _, parsed in entries for model in parsed["observed_models"]})
        if models and task["model"] not in models:
            reasons.append("native_model_mismatch")
        reasons = sorted(set(reasons))
        all_reasons.update(reasons)
        reports.append(
            {
                **task,
                "usage": None if reasons else totals,
                "observed_usage": totals,
                "observed_models": models,
                "model_evidence": "native" if models else "declared",
                "duplicate_events": duplicates,
                "coverage": {
                    "status": "unavailable" if reasons else "measured",
                    "reasons": reasons,
                },
            }
        )
    complete = sum(task["coverage"]["status"] == "measured" for task in reports)
    totals = {
        field: sum(task["observed_usage"][field] for task in reports) for field in TOKEN_FIELDS
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "metric_version": METRIC_VERSION,
        "variant": manifest["variant"],
        "manifest_sha256": manifest_sha,
        "roster_sha256": roster_sha,
        "tasks": reports,
        "sources": source_reports,
        "totals": totals if complete == len(tasks) else None,
        "coverage": {
            "status": "measured" if complete == len(tasks) else "unavailable",
            "complete_tasks": complete,
            "total_tasks": len(tasks),
            "reasons": sorted(all_reasons),
        },
        "cache": {"hits": hits, "misses": misses, "evicted_sources": evicted},
    }


def compare(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], outcomes: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare all registered tasks. Outcome acceptance remains the producer's assertion."""
    from token_burn.comparison import compare as compare_reports

    return compare_reports(baseline, candidate, outcomes)
