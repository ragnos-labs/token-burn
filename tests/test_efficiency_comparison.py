"""Matched comparison must keep the full matrix, quality, settings, and failed attempts."""

import copy

import pytest

from token_burn import usage
from token_burn._local import ContractError, digest


def comparison_inputs():
    tasks = []
    outcomes = []
    for client in ("codex", "claude_code"):
        for repo in ("example/one", "example/two"):
            for round_ in (1, 2):
                for archetype in ("noisy_diagnosis", "bounded_feature", "handoff_resume"):
                    task_id = f"{client}.{repo.split('/')[-1]}.{round_}.{archetype}"
                    tasks.append(
                        {
                            "task_id": task_id,
                            "repository_id": repo,
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
                    )
                    assertion = {
                        "accepted": True,
                        "evidence_level": "accepted_workflow",
                        "evidence_refs": ["synthetic:receipt"],
                    }
                    outcomes.append(
                        {
                            "task_id": task_id,
                            "repository_id": repo,
                            "baseline": copy.deepcopy(assertion),
                            "candidate": copy.deepcopy(assertion),
                        }
                    )
    reports = []
    for variant, amount in (("baseline", 1000), ("candidate", 999)):
        observations = []
        for task in sorted(tasks, key=lambda row: row["task_id"]):
            components = usage.normalize_codex_usage({"input_tokens": amount, "output_tokens": 10})
            observations.append(
                {
                    **task,
                    "usage": components,
                    "observed_models": [],
                    "model_evidence": "declared",
                    "coverage": {"status": "measured", "reasons": []},
                }
            )
        roster = [
            {k: v for k, v in task.items() if k != "sources_complete"}
            for task in sorted(tasks, key=lambda row: row["task_id"])
        ]
        reports.append(
            {
                "schema_version": "token_burn.usage.v1",
                "variant": variant,
                "parser_version": usage.PARSER_VERSION,
                "metric_version": usage.METRIC_VERSION,
                "manifest_sha256": "a" * 64,
                "roster_sha256": digest(roster),
                "tasks": observations,
            }
        )
    return *reports, {
        "schema_version": "token_burn.outcomes.v1",
        "required_groups": [
            {"client": c, "repository_id": r}
            for c in ("codex", "claude_code")
            for r in ("example/one", "example/two")
        ],
        "required_rounds": [1, 2],
        "required_archetypes": ["noisy_diagnosis", "bounded_feature", "handoff_resume"],
        "tasks": outcomes,
    }


def refresh_roster(report):
    report["roster_sha256"] = digest(
        [
            {k: row[k] for k in usage.TASK_FIELDS if k != "sources_complete"}
            for row in sorted(report["tasks"], key=lambda row: row["task_id"])
        ]
    )


def test_any_repeatable_savings_is_accepted_without_ten_percent_target():
    result = usage.compare(*comparison_inputs())
    assert result["accepted"] is True
    assert result["matched_pairs"] == 24
    assert result["coverage"] == {
        "status": "measured",
        "complete_tasks": 24,
        "total_tasks": 24,
        "reasons": [],
    }
    assert result["outcome_evidence"] == "producer_supplied"
    assert len(result["groups"]) == 4
    assert all(row["matched_pairs"] == 3 for group in result["groups"] for row in group["rounds"])
    assert all(
        0 < row["reduction_ratio"] < 0.01 for group in result["groups"] for row in group["rounds"]
    )


def test_regression_in_one_group_round_cannot_hide_in_other_groups():
    baseline, candidate, outcomes = comparison_inputs()
    candidate["tasks"][0]["usage"]["input_tokens"] += 100
    candidate["tasks"][0]["usage"]["processed_tokens"] += 100
    result = usage.compare(baseline, candidate, outcomes)
    assert result["status"] == "not_accepted"
    assert result["matched_pairs"] == 24
    assert any(not group["repeatable_savings"] for group in result["groups"])


@pytest.mark.parametrize("variant", ["baseline", "candidate"])
def test_failed_quality_remains_in_denominator_and_blocks_savings_claim(variant):
    baseline, candidate, outcomes = comparison_inputs()
    outcomes["tasks"][0][variant]["accepted"] = False
    result = usage.compare(baseline, candidate, outcomes)
    assert result["status"] == "not_accepted"
    assert result["quality_status"] == "failed"
    assert result["matched_pairs"] == 24
    assert result["coverage"]["complete_tasks"] == 24


@pytest.mark.parametrize("level", ["component", "integration", "unknown"])
def test_lower_evidence_is_not_accepted_workflow_proof(level):
    baseline, candidate, outcomes = comparison_inputs()
    outcomes["tasks"][0]["candidate"]["evidence_level"] = level
    result = usage.compare(baseline, candidate, outcomes)
    assert result["status"] == "unavailable"
    assert result["quality_status"] == "unavailable"


def test_dropped_failed_task_makes_comparison_unavailable():
    baseline, candidate, outcomes = comparison_inputs()
    candidate["tasks"].pop()
    refresh_roster(candidate)
    result = usage.compare(baseline, candidate, outcomes)
    assert result["accepted"] is False
    assert "task_roster_mismatch" in result["coverage"]["reasons"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "different-model"),
        ("reasoning_effort", "low"),
        ("validation_sha256", "f" * 64),
        ("prompt_sha256", "f" * 64),
        ("revision", "f" * 40),
    ],
)
def test_changed_settings_or_validation_block_comparison(field, value):
    baseline, candidate, outcomes = comparison_inputs()
    candidate["tasks"][0][field] = value
    refresh_roster(candidate)
    result = usage.compare(baseline, candidate, outcomes)
    assert result["status"] == "unavailable"
    assert "task_contract_mismatch" in result["coverage"]["reasons"]


def test_missing_usage_is_never_zero():
    baseline, candidate, outcomes = comparison_inputs()
    candidate["tasks"][0]["usage"] = None
    candidate["tasks"][0]["coverage"] = {"status": "unavailable", "reasons": ["missing_source"]}
    result = usage.compare(baseline, candidate, outcomes)
    assert result["coverage"]["complete_tasks"] == 23
    assert result["status"] == "unavailable"
    assert all(
        row["candidate_tokens"] is None for group in result["groups"] for row in group["rounds"]
    )


def test_same_pr_number_in_different_repositories_is_not_deduplicated():
    baseline, candidate, outcomes = comparison_inputs()
    for row in outcomes["tasks"]:
        row["baseline"]["evidence_refs"] = ["pr:12"]
        row["candidate"]["evidence_refs"] = ["pr:12"]
    result = usage.compare(baseline, candidate, outcomes)
    assert result["matched_pairs"] == 24
    assert result["accepted"] is True
    outcomes["tasks"][0]["repository_id"] = "other/repository"
    assert usage.compare(baseline, candidate, outcomes)["status"] == "unavailable"


def test_duplicate_required_group_or_task_and_bool_count_are_rejected():
    baseline, candidate, outcomes = comparison_inputs()
    outcomes["required_groups"].append(outcomes["required_groups"][0])
    with pytest.raises(ContractError):
        usage.compare(baseline, candidate, outcomes)
    baseline, candidate, outcomes = comparison_inputs()
    candidate["tasks"][0]["usage"]["input_tokens"] = True
    with pytest.raises(ContractError):
        usage.compare(baseline, candidate, outcomes)


def test_parser_change_invalidates_baseline_even_if_totals_match():
    baseline, candidate, outcomes = comparison_inputs()
    candidate["parser_version"] = "next"
    assert usage.compare(baseline, candidate, outcomes)["status"] == "unavailable"


def test_observed_model_changes_cannot_hide_behind_unchanged_manifest_model():
    baseline, candidate, outcomes = comparison_inputs()
    candidate["tasks"][0]["observed_models"] = ["synthetic-model", "different-helper-model"]
    candidate["tasks"][0]["model_evidence"] = "native"
    result = usage.compare(baseline, candidate, outcomes)
    assert result["status"] == "unavailable"
    assert "native_model_evidence_mismatch" in result["coverage"]["reasons"]


def test_context_window_model_is_compared_without_normalization():
    baseline, candidate, outcomes = comparison_inputs()
    for report in (baseline, candidate):
        for item in report["tasks"]:
            item["model"] = "claude-opus-5[1m]"
            item["observed_models"] = [item["model"]]
            item["model_evidence"] = "native"
        refresh_roster(report)
    assert usage.compare(baseline, candidate, outcomes)["accepted"] is True
    candidate["tasks"][0]["observed_models"] = ["claude-opus-5"]
    assert usage.compare(baseline, candidate, outcomes)["accepted"] is False
