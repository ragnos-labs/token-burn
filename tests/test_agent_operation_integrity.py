# SPDX-License-Identifier: Apache-2.0
"""Malformed retained journals cannot become healthy status or exported facts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from token_burn import operations
from token_burn.monitor import snapshot
from token_burn.operations import Operation


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_BURN_STATE_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("TOKEN_BURN_OTEL_ENABLED", "0")


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence", 700),
        ("sequence", True),
        ("trace_id", "b" * 32),
        ("schema_version", "unknown"),
        ("operation", "cleanup.remove"),
        ("outcome", None),
        ("lifecycle", "start"),
        ("timestamp_ns", -1),
        ("event_id", "e" * 32),
        ("private_extra", "must-never-be-exported"),
    ],
)
def test_corrupt_event_is_rejected_by_all_readers(field, value):
    op = Operation.start("evidence.capture")
    terminal = op.finish("completed", exit_code=0)
    path = Path(terminal["receipt"])
    envelope = json.loads(path.read_text())
    envelope["event"][field] = value
    path.write_text(json.dumps(envelope))
    for read in (op.status, op.read):
        with pytest.raises(ValueError):
            read()
    assert snapshot()["complete"] is False


def test_changed_local_event_cannot_export_different_wire_fact(monkeypatch):
    op = Operation.start("evidence.capture")
    terminal = op.finish("failed", reason_code="process_exit", exit_code=9)
    path = Path(terminal["receipt"])
    envelope = json.loads(path.read_text())
    envelope["event"]["outcome"] = "completed"
    path.write_text(json.dumps(envelope))
    sent = []
    monkeypatch.setenv("TOKEN_BURN_OTEL_ENABLED", "1")
    monkeypatch.setattr(operations, "_collector_endpoint", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(
        operations, "_send", lambda *args: sent.append(args) or {"state": "accepted"}
    )
    result = op.export_pending()
    assert not sent
    assert not result["collector_accepted"]
    assert result["error_type"] == "ValueError"


def test_corrupt_replay_returns_error_and_nonzero_exit(capsys):
    from token_burn.history_cli import main

    op = Operation.start("evidence.capture")
    terminal = op.finish("failed", reason_code="process_exit", exit_code=9)
    path = Path(terminal["receipt"])
    envelope = json.loads(path.read_text())
    envelope["event"]["outcome"] = "completed"
    path.write_text(json.dumps(envelope))
    assert main(["replay", op.directory.name]) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "error"
    assert value["error_type"] == "ValueError"
    assert value["collector_accepted"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "a" * 32),
        ("trace_id", "no"),
        ("root_span_id", "c" * 32),
        ("owner_pid", True),
        ("operation", []),
        ("schema_version", "unknown"),
        ("started_ns", -1),
    ],
)
def test_malformed_manifest_does_not_open_as_a_trusted_operation(field, value):
    op = Operation.start("evidence.capture")
    path = op.directory / "run.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        Operation.open(op.directory.name)


def test_dead_unreaped_owner_is_not_reported_as_running(tmp_path):
    import os
    import subprocess
    import sys
    import time

    marker = tmp_path / "owner-run"
    script = (
        "import os,pathlib,sys;from token_burn.operations import Operation;"
        "op=Operation.start('evidence.capture');"
        "pathlib.Path(sys.argv[1]).write_text(op.directory.name);os._exit(0)"
    )
    owner = subprocess.Popen([sys.executable, "-c", script, str(marker)], env=dict(os.environ))
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(owner.pid)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if state.startswith("Z") and marker.exists():
                break
            time.sleep(0.01)
        else:
            pytest.fail("fixture owner did not become an unreaped dead process")
        op = Operation.open(marker.read_text())
        status = op.status()
        assert status["state"] == "terminal_record_missing"
        assert status["owner_alive"] is False and not status["running"]
        assert snapshot()["runs"][0]["state"] == "terminal_record_missing"
    finally:
        owner.wait(timeout=5)


@pytest.mark.parametrize("reason", ["dirty_archive_failed", "recovery_artifact_missing"])
def test_failed_recovery_reaches_actionable_inventory(reason):
    from token_burn.scope import operation_scope

    with operation_scope("cleanup.remove") as scope:
        result = scope.complete({"ok": False, "reason_code": reason})
    op = Operation.open(result["operation"]["run_id"])
    assert op.read()["events"][-1]["reason_code"] == "recovery_unverified"
    assert snapshot()["recovery_unverified"] == 1
