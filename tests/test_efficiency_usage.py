"""Synthetic native evidence, complete attribution, cache reuse, and unavailable states."""

import copy
import json
import os
import subprocess
import sys

import pytest

from token_burn import usage
from token_burn._local import ContractError, digest, encoded, state_lock


def task(task_id="task1", client="codex", round_=1, archetype="repair"):
    return {
        "task_id": task_id,
        "repository_id": "example/project",
        "client": client,
        "model": "synthetic-model",
        "reasoning_effort": "high",
        "revision": "a" * 40,
        "prompt_sha256": "b" * 64,
        "validation_sha256": "c" * 64,
        "round": round_,
        "archetype": archetype,
        "sources_complete": True,
    }


def source(path, source_id="source1", task_id="task1", format_="codex_exec_jsonl", **fields):
    return {
        "source_id": source_id,
        "task_id": task_id,
        "path": str(path),
        "format": format_,
        "kind": "root",
        **fields,
    }


def manifest(tasks, sources, variant="baseline"):
    return {
        "schema_version": "token_burn.sources.v1",
        "variant": variant,
        "tasks": tasks,
        "sources": sources,
    }


def jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def codex_rows(input_=100, output=20, cached=40, session="synthetic-session"):
    return [
        {"type": "thread.started", "thread_id": session},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "PRIVATE-CONTENT"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_,
                "cached_input_tokens": cached,
                "output_tokens": output,
                "reasoning_output_tokens": 5,
            },
        },
    ]


def rollout_row(input_, output, cached, stamp):
    return {
        "type": "event_msg",
        "timestamp": f"2026-01-01T00:00:{stamp:02d}Z",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                }
            },
        },
    }


def test_native_components_count_cache_and_reasoning_once():
    codex = usage.normalize_codex_usage(
        {
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "output_tokens": 20,
            "reasoning_output_tokens": 7,
        }
    )
    claude = usage.normalize_claude_usage(
        {
            "input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30,
            "output_tokens": 8,
            "reasoning_output_tokens": 2,
        }
    )
    assert codex["processed_tokens"] == 120
    assert codex["fresh_input_tokens"] == 60
    assert claude["input_tokens"] == 60
    assert claude["processed_tokens"] == 68
    for value in (True, -1, float("inf"), "100"):
        with pytest.raises(ContractError):
            usage.normalize_codex_usage({"input_tokens": value, "output_tokens": 0})


def test_capture_retry_and_continuation_are_all_attributed_to_task(tmp_path):
    first = jsonl(tmp_path / "first.jsonl", codex_rows())
    retry = jsonl(tmp_path / "retry.jsonl", codex_rows(90, 10, 20, "retry-session"))
    continued = jsonl(tmp_path / "continued.jsonl", codex_rows(60, 8, 10))
    report = usage.collect(
        manifest(
            [task()],
            [
                source(first),
                source(retry, "retry", kind="child"),
                source(continued, "continue", kind="continuation"),
            ],
        ),
        state_dir=tmp_path / "state",
    )
    assert report["coverage"]["status"] == "measured"
    assert report["totals"]["processed_tokens"] == 288
    assert "PRIVATE-CONTENT" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)


def test_cumulative_sources_dedupe_and_child_baseline_never_replays_inherited_work(tmp_path):
    root_rows = [
        {"type": "session_meta", "payload": {"id": "root"}},
        rollout_row(100, 10, 20, 1),
        rollout_row(130, 15, 25, 2),
    ]
    root = jsonl(tmp_path / "root.jsonl", root_rows)
    duplicate = jsonl(tmp_path / "duplicate.jsonl", root_rows)
    child = jsonl(
        tmp_path / "child.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "child", "forked_from_id": "root"}},
            rollout_row(150, 18, 25, 3),
        ],
    )
    sources = [
        source(root, "root", format_="codex_rollout_jsonl"),
        source(duplicate, "duplicate", format_="codex_rollout_jsonl"),
        source(
            child,
            "child",
            format_="codex_rollout_jsonl",
            kind="child",
            initial_usage={"input_tokens": 130, "cached_input_tokens": 25, "output_tokens": 15},
        ),
    ]
    report = usage.collect(manifest([task()], sources), state_dir=tmp_path / "state")
    assert report["coverage"]["status"] == "measured"
    assert report["totals"]["processed_tokens"] == 168
    assert report["tasks"][0]["duplicate_events"] == 2
    del sources[-1]["initial_usage"]
    ambiguous = usage.collect(manifest([task()], sources), state_dir=tmp_path / "state")
    assert ambiguous["totals"] is None
    assert "inherited_usage_baseline_unavailable" in ambiguous["coverage"]["reasons"]


def test_counter_reset_retains_new_usage_without_negative_counts(tmp_path):
    path = jsonl(
        tmp_path / "reset.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "root"}},
            rollout_row(100, 10, 20, 1),
            rollout_row(20, 3, 4, 2),
            rollout_row(25, 4, 5, 3),
        ],
    )
    report = usage.collect(
        manifest([task()], [source(path, format_="codex_rollout_jsonl")]),
        state_dir=tmp_path / "state",
    )
    assert report["totals"]["processed_tokens"] == 139


@pytest.mark.parametrize(
    "component,first,last",
    [
        ("cached_input_tokens", 20, 10),
        ("reasoning_output_tokens", 8, 4),
    ],
)
def test_component_reattribution_never_replays_full_input_output(tmp_path, component, first, last):
    initial = rollout_row(100, 10, 20, 1)
    following = rollout_row(110, 12, 20, 2)
    initial["payload"]["info"]["total_token_usage"][component] = first
    following["payload"]["info"]["total_token_usage"][component] = last
    path = jsonl(
        tmp_path / "reattributed.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "root"}},
            initial,
            following,
        ],
    )
    declared = manifest([task()], [source(path, format_="codex_rollout_jsonl")])
    cold = usage.collect(declared, state_dir=tmp_path / "state")
    warm = usage.collect(declared, state_dir=tmp_path / "state")
    assert cold["coverage"]["status"] == "measured"
    assert cold["totals"]["processed_tokens"] == 122
    assert warm["totals"] == cold["totals"]


def test_one_primary_counter_decrease_is_unavailable_not_an_invented_global_reset(tmp_path):
    path = jsonl(
        tmp_path / "ambiguous.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "root"}},
            rollout_row(100, 10, 20, 1),
            rollout_row(90, 12, 20, 2),
        ],
    )
    report = usage.collect(
        manifest([task()], [source(path, format_="codex_rollout_jsonl")]),
        state_dir=tmp_path / "state",
    )
    assert report["totals"] is None
    assert "primary_counter_reset_ambiguous" in report["coverage"]["reasons"]


def test_claude_message_ids_dedupe_across_transcript_copies(tmp_path):
    row = {
        "type": "assistant",
        "sessionId": "c-session",
        "message": {
            "role": "assistant",
            "id": "native-message",
            "model": "synthetic-model",
            "content": "PRIVATE-CONTENT",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
                "output_tokens": 8,
            },
        },
    }
    one = jsonl(tmp_path / "one.jsonl", [row])
    two = jsonl(tmp_path / "two.jsonl", [row])
    report = usage.collect(
        manifest(
            [task(client="claude_code")],
            [
                source(one, format_="claude_transcript_jsonl"),
                source(two, "copy", format_="claude_transcript_jsonl"),
            ],
        ),
        state_dir=tmp_path / "state",
    )
    assert report["totals"]["processed_tokens"] == 68
    assert report["tasks"][0]["duplicate_events"] == 1


@pytest.mark.parametrize(
    "is_error,model_usage,expected",
    [
        (True, {}, "unavailable"),
        (False, {}, "unavailable"),
        (False, {"synthetic-model": {}}, "measured"),
    ],
)
def test_claude_native_auth_error_is_never_successful_zero(
    tmp_path, is_error, model_usage, expected
):
    path = tmp_path / "result.json"
    path.write_text(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": is_error,
                "session_id": "c-session",
                "result": "PRIVATE-CONTENT",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "modelUsage": model_usage,
            }
        )
    )
    report = usage.collect(
        manifest([task(client="claude_code")], [source(path, format_="claude_print_json")]),
        state_dir=tmp_path / "state",
    )
    assert report["coverage"]["status"] == expected
    if expected == "unavailable":
        assert report["totals"] is None


def test_missing_and_corrupt_sources_preserve_unknown_coverage(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_bytes(b'{"type":"thread.started","thread_id":"synthetic"}\n\xff\n')
    report = usage.collect(
        manifest([task()], [source(path), source(tmp_path / "absent", "missing")]),
        state_dir=tmp_path / "state",
    )
    assert report["coverage"]["status"] == "unavailable"
    assert report["totals"] is None
    assert "invalid_native_json" in report["coverage"]["reasons"]


def test_explicit_incomplete_source_scope_is_not_zero_or_success(tmp_path):
    item = task()
    item["sources_complete"] = False
    report = usage.collect(manifest([item], []), state_dir=tmp_path / "state")
    assert report["tasks"][0]["usage"] is None
    assert report["coverage"]["complete_tasks"] == 0


def test_bounded_cache_reuses_only_matching_parser_manifest_and_content(tmp_path, monkeypatch):
    path = jsonl(tmp_path / "source.jsonl", codex_rows())
    declared = manifest([task()], [source(path)])
    first = usage.collect(declared, state_dir=tmp_path / "state")
    original = usage._parse
    monkeypatch.setattr(usage, "_parse", lambda *_: pytest.fail("warm cache reparsed a source"))
    second = usage.collect(declared, state_dir=tmp_path / "state")
    assert second["totals"] == first["totals"]
    assert second["cache"]["hits"] == 1
    assert (tmp_path / "state" / "usage-cache.json").stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(usage, "_parse", original)
    jsonl(path, codex_rows(101))
    third = usage.collect(declared, state_dir=tmp_path / "state")
    assert third["cache"]["misses"] == 1
    assert third["totals"]["processed_tokens"] == 121
    monkeypatch.setattr(usage, "MAX_CACHE_BYTES", 500)
    compacted = usage.collect(declared, state_dir=tmp_path / "other-state")
    assert compacted["totals"] == third["totals"]
    assert compacted["cache"]["evicted_sources"] == 1
    assert (tmp_path / "other-state" / "usage-cache.json").stat().st_size <= 500


def test_competing_foreground_collector_fails_busy_and_cannot_steal_lock(tmp_path):
    state = tmp_path / "state"
    with state_lock(state):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from token_burn._local import state_lock; from pathlib import Path; "
                "\nwith state_lock(Path(__import__('sys').argv[1])): print('stolen')",
                str(state),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "collector_busy" in result.stderr
        assert "stolen" not in result.stdout
    with state_lock(state):
        pass


def test_source_budgets_degrade_and_never_produce_zero_savings(tmp_path, monkeypatch):
    path = jsonl(tmp_path / "source.jsonl", codex_rows())
    monkeypatch.setattr(usage, "MAX_SOURCE_BYTES", 10)
    report = usage.collect(manifest([task()], [source(path)]), state_dir=tmp_path / "state")
    assert report["totals"] is None
    assert report["sources"][0]["content_sha256"] is None


def test_duplicate_manifest_source_range_is_rejected(tmp_path):
    path = tmp_path / "source.jsonl"
    with pytest.raises(ContractError, match="duplicate_source_range"):
        usage.validate_manifest(manifest([task()], [source(path), source(path, "other")]))


def test_manifest_identity_binds_exact_source_selection(tmp_path):
    path = jsonl(tmp_path / "source.jsonl", codex_rows())
    declared = manifest([task()], [source(path)])
    report = usage.collect(declared, state_dir=tmp_path / "state")
    assert report["manifest_sha256"] == digest(declared)
    assert (
        report["sources"][0]["content_sha256"]
        == __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    )
    modified = copy.deepcopy(declared)
    modified["sources"][0]["end_line"] = 2
    partial = usage.collect(modified, state_dir=tmp_path / "state")
    assert partial["manifest_sha256"] != report["manifest_sha256"]
    assert partial["totals"] is None
    assert str(tmp_path).encode() not in encoded(report)


def test_private_state_directory_is_required(tmp_path):
    directory = tmp_path / "public-state"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    with pytest.raises(ContractError, match="state_directory_not_private"):
        usage.collect(manifest([task()], []), state_dir=directory)


def test_corrupt_cache_entry_is_reparsed_without_using_modified_counts(tmp_path):
    path = jsonl(tmp_path / "source.jsonl", codex_rows())
    declared = manifest([task()], [source(path)])
    first = usage.collect(declared, state_dir=tmp_path / "state")
    cache_path = tmp_path / "state" / "usage-cache.json"
    cache = json.loads(cache_path.read_text())
    next(iter(cache["entries"].values()))["payload"]["events"][0]["usage"]["input_tokens"] = 0
    cache_path.write_text(json.dumps(cache))
    second = usage.collect(declared, state_dir=tmp_path / "state")
    assert second["totals"] == first["totals"]
    assert second["cache"]["misses"] == 1


def test_cumulative_range_retains_identity_and_excludes_earlier_usage(tmp_path):
    path = jsonl(
        tmp_path / "selected.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "root"}},
            rollout_row(100, 10, 20, 1),
            rollout_row(130, 15, 25, 2),
        ],
    )
    declared = manifest(
        [task()],
        [
            source(
                path,
                format_="codex_rollout_jsonl",
                start_line=3,
                kind="continuation",
                initial_usage={"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10},
            )
        ],
    )
    report = usage.collect(declared, state_dir=tmp_path / "state")
    assert report["coverage"]["status"] == "measured"
    assert report["totals"]["processed_tokens"] == 35


def test_native_capture_cannot_be_counted_as_two_distinct_tasks(tmp_path):
    path = jsonl(tmp_path / "source.jsonl", codex_rows())
    copied = tmp_path / "copied.jsonl"
    copied.write_bytes(path.read_bytes())
    declared = manifest(
        [task(), task("task2")], [source(path), source(copied, "source2", task_id="task2")]
    )
    report = usage.collect(declared, state_dir=tmp_path / "state")
    assert report["coverage"]["complete_tasks"] == 0
    assert "ambiguous_task_attribution" in report["coverage"]["reasons"]


def test_same_session_aggregate_and_detail_formats_are_unavailable(tmp_path):
    aggregate = jsonl(tmp_path / "aggregate.jsonl", codex_rows(session="root"))
    detail = jsonl(
        tmp_path / "detail.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "root"}},
            rollout_row(100, 20, 40, 1),
        ],
    )
    report = usage.collect(
        manifest(
            [task()], [source(aggregate), source(detail, "detail", format_="codex_rollout_jsonl")]
        ),
        state_dir=tmp_path / "state",
    )
    assert "overlapping_native_formats" in report["coverage"]["reasons"]
    assert report["totals"] is None


@pytest.mark.parametrize(
    "client,format_",
    [
        ("codex", "codex_exec_jsonl"),
        ("claude_code", "claude_print_json"),
    ],
)
def test_reformatted_native_aggregate_replay_is_not_a_second_attempt(tmp_path, client, format_):
    rows = (
        codex_rows()
        if client == "codex"
        else [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "same-session",
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "modelUsage": {"synthetic-model": {}},
            }
        ]
    )
    first = tmp_path / "first.jsonl"
    second = tmp_path / "reformatted.jsonl"
    first.write_text("".join(json.dumps(row) + "\n" for row in rows))
    second.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    assert first.read_bytes() != second.read_bytes()
    report = usage.collect(
        manifest(
            [task(client=client)],
            [source(first, format_=format_), source(second, "replay", format_=format_)],
        ),
        state_dir=tmp_path / "state",
    )
    assert report["totals"] is None
    assert "duplicate_native_source" in report["coverage"]["reasons"]


def test_distinct_native_attempts_with_equal_usage_are_both_counted(tmp_path):
    first = jsonl(tmp_path / "first.jsonl", codex_rows(session="first-attempt"))
    second = jsonl(tmp_path / "retry.jsonl", codex_rows(session="real-retry"))
    report = usage.collect(
        manifest([task()], [source(first), source(second, "retry")]), state_dir=tmp_path / "state"
    )
    assert report["coverage"]["status"] == "measured"
    assert report["totals"]["processed_tokens"] == 240


def test_selected_cumulative_range_requires_baseline_even_when_labeled_root(tmp_path):
    path = jsonl(
        tmp_path / "selected.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "root"}},
            rollout_row(100, 10, 20, 1),
            rollout_row(130, 15, 25, 2),
        ],
    )
    declared = manifest([task()], [source(path, format_="codex_rollout_jsonl", start_line=3)])
    report = usage.collect(declared, state_dir=tmp_path / "state")
    assert report["totals"] is None
    assert "inherited_usage_baseline_unavailable" in report["coverage"]["reasons"]


def test_partial_semantic_aggregate_replay_is_also_unavailable(tmp_path):
    first_rows = codex_rows()
    first_rows.append({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 5}})
    first = jsonl(tmp_path / "full.jsonl", first_rows)
    partial = jsonl(tmp_path / "partial.jsonl", [first_rows[0], first_rows[-1]])
    report = usage.collect(
        manifest([task()], [source(first), source(partial, "partial")]),
        state_dir=tmp_path / "state",
    )
    assert report["totals"] is None
    assert "duplicate_native_source" in report["coverage"]["reasons"]


@pytest.mark.parametrize("format_", ["claude_print_json", "claude_transcript_jsonl"])
def test_native_context_window_model_identity_is_preserved(tmp_path, format_):
    model = "claude-opus-5[1m]"
    native_usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 30,
        "output_tokens": 8,
    }
    row = (
        {
            "type": "result",
            "is_error": False,
            "session_id": "model-session",
            "usage": native_usage,
            "modelUsage": {model: {}},
        }
        if format_ == "claude_print_json"
        else {
            "type": "assistant",
            "sessionId": "model-session",
            "message": {
                "role": "assistant",
                "id": "model-message",
                "model": model,
                "usage": native_usage,
            },
        }
    )
    path = tmp_path / "native.json"
    path.write_text(json.dumps(row) + "\n")
    contract = task(client="claude_code")
    contract["model"] = model
    selected = manifest([contract], [source(path, format_=format_)])
    cold = usage.collect(selected, state_dir=tmp_path / "state")
    warm = usage.collect(selected, state_dir=tmp_path / "state")
    for report in (cold, warm):
        assert report["coverage"]["status"] == "measured"
        assert report["totals"]["processed_tokens"] == 68
        assert report["tasks"][0]["observed_models"] == [model]
    contract["model"] = "claude-opus-5"
    mismatch = usage.collect(selected, state_dir=tmp_path / "other-state")
    assert mismatch["coverage"]["status"] == "unavailable"
    assert "native_model_mismatch" in mismatch["tasks"][0]["coverage"]["reasons"]


@pytest.mark.parametrize(
    "model",
    [
        "model[]",
        "model[1m",
        "model1m]",
        "model[1m][1m]",
        "model[0m]",
        "model[1m]\n",
        "model[../x]",
        "a" * 161,
    ],
)
def test_malformed_model_qualifiers_are_rejected(model):
    contract = task()
    contract["model"] = model
    with pytest.raises(ContractError):
        usage.validate_manifest(manifest([contract], []))
