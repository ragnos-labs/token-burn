"""Offline synthetic collection/comparison example. No model savings are measured."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from token_burn._local import write_json
from token_burn.handoff import create
from token_burn.profile import show
from token_burn.usage import collect, compare


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    directory = parser.parse_args().output_dir.absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    tasks = [
        {
            "task_id": f"{client}-repair-{round_}",
            "repository_id": "example/project",
            "client": client,
            "model": "synthetic-model",
            "reasoning_effort": "high",
            "revision": "a" * 40,
            "prompt_sha256": "b" * 64,
            "validation_sha256": "c" * 64,
            "round": round_,
            "archetype": "repair",
            "sources_complete": True,
        }
        for client in ("codex", "claude_code")
        for round_ in (1, 2)
    ]
    reports = {}
    for variant, token_count in (("baseline", 100), ("candidate", 99)):
        sources = []
        for task in tasks:
            source_id = f"{variant}-{task['task_id']}"
            path = directory / f"{source_id}.jsonl"
            if task["client"] == "codex":
                rows = [
                    {"type": "thread.started", "thread_id": source_id},
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": token_count,
                            "cached_input_tokens": 20,
                            "output_tokens": 5,
                        },
                    },
                ]
                format_name = "codex_exec_jsonl"
            else:
                rows = [
                    {
                        "type": "assistant",
                        "sessionId": source_id,
                        "message": {
                            "role": "assistant",
                            "id": source_id,
                            "model": "synthetic-model",
                            "usage": {
                                "input_tokens": token_count - 20,
                                "cache_read_input_tokens": 20,
                                "output_tokens": 5,
                            },
                        },
                    }
                ]
                format_name = "claude_transcript_jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            path.chmod(0o600)
            sources.append(
                {
                    "source_id": source_id,
                    "task_id": task["task_id"],
                    "path": str(path),
                    "format": format_name,
                    "kind": "root",
                }
            )
        manifest = {
            "schema_version": "token_burn.sources.v1",
            "variant": variant,
            "tasks": tasks,
            "sources": sources,
        }
        write_json(directory / f"{variant}-sources.json", manifest)
        reports[variant] = collect(manifest, state_dir=directory / f"{variant}-state")
        write_json(directory / f"{variant}.json", reports[variant])
    outcomes = {
        "schema_version": "token_burn.outcomes.v1",
        "required_groups": [
            {"client": client, "repository_id": "example/project"}
            for client in ("codex", "claude_code")
        ],
        "required_rounds": [1, 2],
        "required_archetypes": ["repair"],
        "tasks": [
            {
                "task_id": task["task_id"],
                "repository_id": task["repository_id"],
                **{
                    variant: {
                        "accepted": True,
                        "evidence_level": "accepted_workflow",
                        "evidence_refs": [f"synthetic:{variant}-{task['task_id']}"],
                    }
                    for variant in ("baseline", "candidate")
                },
            }
            for task in tasks
        ],
    }
    write_json(directory / "outcomes.json", outcomes)
    comparison = compare(reports["baseline"], reports["candidate"], outcomes)
    write_json(directory / "comparison.json", comparison)
    write_json(directory / "profile.json", show())
    write_json(
        directory / "handoff.json",
        create(
            {
                "objective": "Inspect this synthetic demonstration",
                "authority": {"actions": ["read"]},
                "stop_conditions": ["Real model calls are outside this example"],
                "changed_files": [],
                "test_state": {"status": "synthetic_fixture"},
                "receipts": ["synthetic:comparison"],
                "active_processes": [],
                "blockers": [],
                "next_action": "Read the efficiency contract",
            }
        ),
    )
    print(
        json.dumps(
            {
                "example": "synthetic_only",
                "comparison_status": comparison["status"],
                "matched_pairs": comparison["matched_pairs"],
                "measured_model_savings": False,
            }
        )
    )


if __name__ == "__main__":
    main()
