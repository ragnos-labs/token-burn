# SPDX-License-Identifier: Apache-2.0
"""Compare complete registered tasks without discarding failed attempts or outcomes."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from token_burn._local import ContractError, digest
from token_burn.usage import (
    MAX_TASKS,
    METRIC_VERSION,
    PARSER_VERSION,
    TASK_FIELDS,
    TOKEN_FIELDS,
    _hex,
    _integer,
    _label,
    _model,
    _repository,
)


def _outcomes(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema_version", "required_groups", "required_rounds", "required_archetypes", "tasks"}
        or value.get("schema_version") != "token_burn.outcomes.v1"
    ):
        raise ContractError("invalid_outcomes_schema")
    groups, rounds, archetypes, tasks = (
        value[k] for k in ("required_groups", "required_rounds", "required_archetypes", "tasks")
    )
    if not isinstance(groups, list) or not 1 <= len(groups) <= 16:
        raise ContractError("invalid_required_groups")
    group_keys = []
    for group in groups:
        if (
            not isinstance(group, dict)
            or set(group) != {"client", "repository_id"}
            or group["client"] not in {"codex", "claude_code"}
            or not _repository(group["repository_id"])
        ):
            raise ContractError("invalid_required_groups")
        group_keys.append((group["client"], group["repository_id"]))
    if len(set(group_keys)) != len(group_keys):
        raise ContractError("duplicate_required_group")
    if (
        not isinstance(rounds, list)
        or not 2 <= len(rounds) <= 8
        or any(type(r) is not int or r < 1 for r in rounds)
        or len(set(rounds)) != len(rounds)
    ):
        raise ContractError("invalid_required_rounds")
    if (
        not isinstance(archetypes, list)
        or not 1 <= len(archetypes) <= 16
        or any(not _label(a) for a in archetypes)
        or len(set(archetypes)) != len(archetypes)
    ):
        raise ContractError("invalid_required_archetypes")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ContractError("invalid_outcome_roster")
    seen = set()
    for task in tasks:
        if (
            not isinstance(task, dict)
            or set(task) != {"task_id", "repository_id", "baseline", "candidate"}
            or not _label(task["task_id"])
            or task["task_id"] in seen
            or not _repository(task["repository_id"])
        ):
            raise ContractError("invalid_outcome_task")
        seen.add(task["task_id"])
        for variant in ("baseline", "candidate"):
            outcome = task[variant]
            if (
                not isinstance(outcome, dict)
                or set(outcome) != {"accepted", "evidence_level", "evidence_refs"}
                or type(outcome["accepted"]) is not bool
                or outcome["evidence_level"]
                not in {"accepted_workflow", "component", "integration", "unknown"}
                or not isinstance(outcome["evidence_refs"], list)
                or len(outcome["evidence_refs"]) > 32
                or any(not _label(ref) for ref in outcome["evidence_refs"])
            ):
                raise ContractError("invalid_outcome_assertion")
    return value


def _report(value: Any, variant: str) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "token_burn.usage.v1"
        or value.get("variant") != variant
        or not isinstance(value.get("tasks"), list)
        or not 1 <= len(value["tasks"]) <= MAX_TASKS
        or any(not _hex(value.get(k)) for k in ("manifest_sha256", "roster_sha256"))
    ):
        raise ContractError("invalid_usage_report")
    result = {}
    for task in value["tasks"]:
        if (
            not isinstance(task, dict)
            or not TASK_FIELDS <= set(task)
            or not _model(task.get("model"))
            or not _label(task.get("task_id"))
            or task["task_id"] in result
            or not _repository(task["repository_id"])
            or task["client"] not in {"codex", "claude_code"}
            or any(not _label(task[k]) for k in ("reasoning_effort", "archetype"))
            or type(task["round"]) is not int
            or task["round"] < 1
            or not _hex(task["revision"], (40, 64))
            or any(not _hex(task[k]) for k in ("prompt_sha256", "validation_sha256"))
            or type(task["sources_complete"]) is not bool
            or not isinstance(task.get("observed_models"), list)
            or len(task["observed_models"]) > 32
            or any(not _model(model) for model in task["observed_models"])
            or task.get("model_evidence") not in {"native", "declared"}
        ):
            raise ContractError("invalid_usage_task")
        coverage = task.get("coverage")
        if (
            not isinstance(coverage, dict)
            or set(coverage) != {"status", "reasons"}
            or coverage["status"] not in {"measured", "unavailable"}
            or not isinstance(coverage["reasons"], list)
            or any(not _label(reason) for reason in coverage["reasons"])
        ):
            raise ContractError("invalid_usage_coverage")
        usage = task.get("usage")
        if usage is not None:
            if not isinstance(usage, dict) or set(usage) != set(TOKEN_FIELDS):
                raise ContractError("invalid_usage_components")
            for count in usage.values():
                _integer(count)
            if usage["processed_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
                raise ContractError("invalid_processed_token_total")
        if coverage["status"] == "measured" and (
            usage is None or coverage["reasons"] or task["sources_complete"] is not True
        ):
            raise ContractError("inconsistent_usage_coverage")
        result[task["task_id"]] = task
    roster = [
        {k: task[k] for k in TASK_FIELDS if k != "sources_complete"}
        for task in sorted(result.values(), key=lambda row: row["task_id"])
    ]
    if digest(roster) != value["roster_sha256"]:
        raise ContractError("usage_roster_digest_mismatch")
    return result


def _quality(outcome: Mapping[str, Any]) -> str:
    assertions = (outcome["baseline"], outcome["candidate"])
    if any(row["accepted"] is False for row in assertions):
        return "failed"
    if any(
        row["evidence_level"] != "accepted_workflow" or not row["evidence_refs"]
        for row in assertions
    ):
        return "unavailable"
    return "passed"


def _combined_quality(values: list[str]) -> str:
    if not values or "unavailable" in values:
        return "unavailable"
    return "failed" if "failed" in values else "passed"


def compare(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], outcomes: Mapping[str, Any]
) -> dict[str, Any]:
    """Require improvement in every declared group and round at unchanged accepted quality."""
    outcomes = _outcomes(outcomes)
    base_tasks, candidate_tasks = _report(baseline, "baseline"), _report(candidate, "candidate")
    expected = {row["task_id"]: row for row in outcomes["tasks"]}
    reasons = set()
    if any(
        report.get("parser_version") != PARSER_VERSION
        or report.get("metric_version") != METRIC_VERSION
        for report in (baseline, candidate)
    ):
        reasons.add("measurement_version_mismatch")
    if set(base_tasks) != set(candidate_tasks) or set(base_tasks) != set(expected):
        reasons.add("task_roster_mismatch")
    if baseline["roster_sha256"] != candidate["roster_sha256"]:
        reasons.add("task_contract_mismatch")
    groups = {(row["client"], row["repository_id"]) for row in outcomes["required_groups"]}
    rounds = set(outcomes["required_rounds"])
    archetypes = set(outcomes["required_archetypes"])
    expected_cells = {
        (client, repo, round_, archetype)
        for client, repo in groups
        for round_ in rounds
        for archetype in archetypes
    }
    seen_cells: Counter[tuple[Any, ...]] = Counter()
    complete_tasks = 0
    qualities = []
    for task_id, outcome in expected.items():
        base, other = base_tasks.get(task_id), candidate_tasks.get(task_id)
        qualities.append(_quality(outcome))
        if base is None or other is None:
            continue
        if any(base[k] != other[k] for k in TASK_FIELDS if k != "sources_complete"):
            reasons.add("task_contract_mismatch")
        if (
            base["observed_models"] != other["observed_models"]
            or base["model_evidence"] != other["model_evidence"]
        ):
            reasons.add("native_model_evidence_mismatch")
        if base["repository_id"] != outcome["repository_id"]:
            reasons.add("outcome_repository_mismatch")
        seen_cells[(base["client"], base["repository_id"], base["round"], base["archetype"])] += 1
        if all(row["coverage"]["status"] == "measured" for row in (base, other)):
            complete_tasks += 1
        else:
            reasons.add("usage_coverage_unavailable")
    if set(seen_cells) != expected_cells or any(count != 1 for count in seen_cells.values()):
        reasons.add("registered_matrix_incomplete")
    all_quality = _combined_quality(qualities)
    report_groups = []
    for client, repo in sorted(groups):
        report_rounds = []
        for round_ in sorted(rounds):
            selected = [
                task_id
                for task_id in expected
                if task_id in base_tasks
                and (
                    base_tasks[task_id]["client"],
                    base_tasks[task_id]["repository_id"],
                    base_tasks[task_id]["round"],
                )
                == (client, repo, round_)
            ]
            quality = _combined_quality([_quality(expected[task_id]) for task_id in selected])
            complete = (
                not reasons
                and len(selected) == len(archetypes)
                and all(
                    task_id in candidate_tasks
                    and base_tasks[task_id]["coverage"]["status"] == "measured"
                    and candidate_tasks[task_id]["coverage"]["status"] == "measured"
                    for task_id in selected
                )
            )
            baseline_tokens = (
                sum(base_tasks[task_id]["usage"]["processed_tokens"] for task_id in selected)
                if complete
                else None
            )
            candidate_tokens = (
                sum(candidate_tasks[task_id]["usage"]["processed_tokens"] for task_id in selected)
                if complete
                else None
            )
            reduction = baseline_tokens - candidate_tokens if complete else None
            ratio = (reduction / baseline_tokens) if complete and baseline_tokens else None
            status = (
                "unavailable"
                if not complete or quality == "unavailable"
                else ("improved" if quality == "passed" and reduction > 0 else "not_improved")
            )
            report_rounds.append(
                {
                    "round": round_,
                    "matched_pairs": len(selected),
                    "baseline_tokens": baseline_tokens,
                    "candidate_tokens": candidate_tokens,
                    "baseline_tokens_per_task": baseline_tokens / len(selected)
                    if complete
                    else None,
                    "candidate_tokens_per_task": candidate_tokens / len(selected)
                    if complete
                    else None,
                    "reduction_tokens": reduction,
                    "reduction_ratio": ratio,
                    "quality_status": quality,
                    "coverage_status": "measured" if complete else "unavailable",
                    "status": status,
                }
            )
        report_groups.append(
            {
                "client": client,
                "repository_id": repo,
                "repeatable_savings": all(row["status"] == "improved" for row in report_rounds),
                "rounds": report_rounds,
            }
        )
    accepted = (
        not reasons
        and all_quality == "passed"
        and all(group["repeatable_savings"] for group in report_groups)
    )
    status = (
        "accepted"
        if accepted
        else ("unavailable" if reasons or all_quality == "unavailable" else "not_accepted")
    )
    return {
        "schema_version": "token_burn.comparison.v1",
        "parser_version": PARSER_VERSION,
        "metric_version": METRIC_VERSION,
        "baseline_sha256": digest(baseline),
        "candidate_sha256": digest(candidate),
        "outcomes_sha256": digest(outcomes),
        "status": status,
        "accepted": accepted,
        "quality_status": all_quality,
        "coverage": {
            "status": "unavailable" if reasons else "measured",
            "complete_tasks": complete_tasks,
            "total_tasks": len(expected),
            "reasons": sorted(reasons),
        },
        "matched_pairs": len(set(expected) & set(base_tasks) & set(candidate_tasks)),
        "groups": report_groups,
        "outcome_evidence": "producer_supplied",
    }
