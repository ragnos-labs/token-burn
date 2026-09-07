"""Protected handoff state, exact byte bounds, installed profile, and CLI privacy."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from token_burn import handoff, profile
from token_burn._local import ContractError, encoded


def state():
    return {
        "objective": "Complete the synthetic repair",
        "authority": {"scope": ["example/project"], "actions": ["edit"]},
        "stop_conditions": ["Unexpected scope"],
        "changed_files": ["src/example.py"],
        "test_state": {"status": "pending"},
        "receipts": ["synthetic:evidence"],
        "active_processes": [],
        "blockers": [],
        "next_action": "Run the required local check",
    }


def test_handoff_preserves_protected_fields_and_operation_ids_separately():
    supplied = state()
    correlation = {
        "legacy": {"mission_id": "mission_" + "d" * 24},
        "operation": {"run_id": "a" * 32, "trace_id": "b" * 32, "root_span_id": "c" * 16},
    }
    packet = handoff.create(supplied, correlation=correlation)
    assert packet["state"] == supplied
    assert packet["correlation"] == correlation
    assert packet["bytes"] == len(encoded(packet))
    assert handoff.validate(packet) == packet
    supplied["authority"]["actions"].append("new-action")
    assert packet["state"]["authority"]["actions"] == ["edit"]


def test_missing_unknown_and_oversized_state_is_never_silently_dropped():
    for field in handoff.PROTECTED_FIELDS:
        supplied = state()
        del supplied[field]
        with pytest.raises(ContractError):
            handoff.create(supplied)
    supplied = state()
    supplied["extra"] = "keep this"
    with pytest.raises(ContractError):
        handoff.create(supplied)
    supplied = state()
    supplied["objective"] = "x" * 32768
    with pytest.raises(ContractError, match="handoff_too_large"):
        handoff.create(supplied)


@pytest.mark.parametrize(
    "path",
    ["../escape", "/tmp/private", "~/.secret", "C:\\secret", "folder/../escape", "bad\nname"],
)
def test_changed_files_must_be_repository_relative(path):
    supplied = state()
    supplied["changed_files"] = [path]
    with pytest.raises(ContractError):
        handoff.create(supplied)


def test_tampered_size_and_wrong_schema_are_rejected():
    packet = handoff.create(state())
    packet["bytes"] += 1
    with pytest.raises(ContractError, match="handoff_integrity_mismatch"):
        handoff.validate(packet)
    packet["schema_version"] = "unknown"
    with pytest.raises(ContractError, match="invalid_handoff_schema"):
        handoff.validate(packet)


def test_seal_retains_legacy_envelope_and_excludes_newline_when_requested():
    legacy = {
        "schema_version": "token_burn_handoff/v2",
        "state": {"owner": "unchanged"},
        "correlation": {"old": "format"},
    }
    packed = handoff.seal(legacy, trailing_newline=False, inclusive_preferred=True)
    assert packed["schema_version"] == legacy["schema_version"]
    assert packed["state"] == legacy["state"]
    assert packed["bytes"] == len(encoded(packed)) - 1
    # The flag changes JSON width by one; no preference boundary may oscillate.
    for target in range(packed["bytes"] - 2, packed["bytes"] + 3):
        result = handoff.seal(
            legacy, preferred_bytes=target, inclusive_preferred=True, trailing_newline=False
        )
        assert result["bytes"] == len(encoded(result)) - 1
    with pytest.raises(ContractError):
        handoff.seal({"data": "x" * 40000}, max_bytes=100000)


def test_released_profile_is_bounded_and_digest_binds_exact_resource():
    record = profile.show()
    assert record["profile_version"] == "0.0.11"
    assert record["runtime_version"] == "0.0.12"
    assert record["profile_id"] == "coding-efficiency"
    assert len(record["document"].encode()) <= 4096
    assert hashlib.sha256(record["document"].encode()).hexdigest() == record["document_sha256"]


def test_cli_writes_immutable_private_handoff_and_does_not_print_its_contents(tmp_path):
    supplied = tmp_path / "input.json"
    private = state()
    private["objective"] = "PRIVATE-HANDOFF-OBJECTIVE"
    supplied.write_text(json.dumps({"state": private}))
    output = tmp_path / "output.json"
    command = [
        sys.executable,
        "-m",
        "token_burn",
        "handoff",
        "create",
        "--input",
        str(supplied),
        "--output",
        str(output),
    ]
    first = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert first.returncode == 0
    assert "PRIVATE-HANDOFF-OBJECTIVE" not in first.stdout + first.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    before = output.read_bytes()
    second = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert second.returncode == 2
    assert output.read_bytes() == before
    validated = subprocess.run(
        [sys.executable, "-m", "token_burn", "handoff", "validate", str(output)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(validated.stdout)["status"] == "valid"


@pytest.mark.parametrize("family", ["usage", "handoff", "profile"])
def test_new_cli_errors_do_not_echo_private_arguments(family):
    marker = "PRIVATE-ARGUMENT-" + "x" * 20000
    result = subprocess.run(
        [sys.executable, "-m", "token_burn", family, "--" + marker],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert marker not in result.stdout + result.stderr
    assert len(result.stdout + result.stderr) < 512


def test_offline_example_and_cli_comparison_work_from_a_foreign_directory(tmp_path):
    script = Path(__file__).resolve().parents[1] / "examples" / "efficiency_demo.py"
    output = tmp_path / "synthetic"
    demo = subprocess.run(
        [sys.executable, str(script), "--output-dir", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert demo.returncode == 0, demo.stderr
    assert json.loads(demo.stdout) == {
        "example": "synthetic_only",
        "comparison_status": "accepted",
        "matched_pairs": 4,
        "measured_model_savings": False,
    }
    compared = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_burn",
            "usage",
            "compare",
            "--baseline",
            str(output / "baseline.json"),
            "--candidate",
            str(output / "candidate.json"),
            "--outcomes",
            str(output / "outcomes.json"),
            "--output",
            str(output / "comparison-cli.json"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert compared.returncode == 0, compared.stderr
    assert json.loads(compared.stdout)["accepted"] is True
    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_burn",
            "usage",
            "collect",
            "--sources",
            str(output / "baseline-sources.json"),
            "--state-dir",
            str(output / "baseline-state"),
            "--output",
            str(output / "baseline-cli.json"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert collected.returncode == 0, collected.stderr
    assert json.loads(collected.stdout)["coverage"]["complete_tasks"] == 4
    assert str(output) not in collected.stdout + compared.stdout
