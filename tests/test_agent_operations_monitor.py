# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import fcntl
import json
import time

import pytest

from token_burn import monitor, operations
from token_burn.history_cli import main


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_BURN_STATE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("TOKEN_BURN_OTEL_ENABLED", "0")


def test_inventory_is_private_read_only_and_running_is_not_missing_terminal(monkeypatch):
    op = operations.Operation.start("evidence.capture", source_ref="PRIVATE/PATH TOKEN")
    before = {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in op.directory.rglob("*")
        if p.is_file()
    }
    value = monitor.snapshot()
    assert value["complete"] and value["counts"]["running"] == 1
    assert value["missing_terminal_owner_dead"] == 0
    text = monitor.metrics_text(value)
    assert op.correlation()["run_id"] not in text
    assert "PRIVATE" not in json.dumps(value) and "owner_pid" not in json.dumps(value)
    assert str(op.directory) not in json.dumps(value)
    assert 'state="running"} 1' in text
    assert before == {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in op.directory.rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(monitor, "_process_identity", lambda _: (False, None))
    value = monitor.snapshot()
    assert value["missing_terminal_owner_dead"] == 1
    assert value["counts"]["running"] == 0
    monkeypatch.setattr(monitor, "_process_identity", lambda _: (None, None))
    value = monitor.snapshot()
    assert value["counts"]["owner_state_unknown"] == 1
    assert value["missing_terminal_owner_dead"] == 0


def test_actionable_reasons_and_ordinary_terminal_results_are_distinct():
    for outcome, reason in [
        ("cancelled", "interrupted"),
        ("blocked", "admission_refused"),
        ("failed", "process_exit"),
        ("process_stopped", "process_signal"),
    ]:
        operations.Operation.start("evidence.capture").finish(outcome, reason_code=reason)
    value = monitor.snapshot()
    assert (
        value["missing_terminal_owner_dead"]
        == value["child_not_reaped"]
        == value["recovery_unverified"]
        == 0
    )
    operations.Operation.start("evidence.capture").finish(
        "cancelled", reason_code="child_not_reaped"
    )
    operations.Operation.start("cleanup.remove").finish(
        "blocked", reason_code="recovery_unverified"
    )
    value = monitor.snapshot()
    assert value["child_not_reaped"] == value["recovery_unverified"] == 1


def test_pending_age_and_held_delivery_are_preserved(monkeypatch):
    now = time.time_ns()
    with monkeypatch.context() as m:
        m.setattr(operations.time, "time_ns", lambda: now - 1000_000_000_000)
        op = operations.Operation.start("evidence.capture")
    event = op.read()["events"][0]
    operations._write(
        op.directory / "delivery" / (event["event_id"] + ".json"),
        {"traces": {"state": "held", "error_type": "SECRET"}},
    )
    value = monitor.snapshot()
    assert value["pending_signals"] == value["held_signals"] == 1
    assert value["oldest_pending_age_seconds"] >= 1000
    assert "SECRET" not in json.dumps(value)


def test_absent_truncated_and_unreadable_never_report_complete(tmp_path):
    assert not monitor.snapshot()["complete"]
    assert not (tmp_path / "private").exists()
    op = operations.Operation.start("evidence.capture")
    operations.Operation.start("evidence.cases")
    limited = monitor.snapshot(limit=1)
    assert limited["truncated"] and not limited["complete"]
    op.stage("spawn")
    limited = monitor.snapshot(max_events=1)
    assert limited["truncated"] and not limited["complete"]
    (op.directory / "run.json").write_text("invalid SECRET JSON")
    value = monitor.snapshot()
    assert value["unreadable_entries"] == 1 and not value["complete"]
    assert "SECRET" not in json.dumps(value)


def test_busy_or_missing_lock_is_read_only_and_does_not_block():
    op = operations.Operation.start("cleanup.remove")
    lock = op.directory / ".journal.lock"
    with lock.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        started = time.monotonic()
        value = monitor.snapshot()
        assert time.monotonic() - started < 0.5
        assert not value["complete"] and value["error_types"] == ["BusyRun"]
    lock.unlink()
    assert not monitor.snapshot()["complete"]
    assert not lock.exists()


def test_untrusted_entry_names_and_symlinks_are_not_exposed(tmp_path):
    op = operations.Operation.start("cleanup.remove")
    (op.directory.parent / "SECRET-PATH").symlink_to(tmp_path, target_is_directory=True)
    value = monitor.snapshot()
    assert not value["complete"] and value["unreadable_entries"] == 1
    assert "SECRET-PATH" not in json.dumps(value)


def test_cli_metrics_and_snapshot_never_export(capsys, monkeypatch):
    op = operations.Operation.start("evidence.capture")
    monkeypatch.setattr(
        operations.Operation, "export_pending", lambda *_a, **_k: pytest.fail("network")
    )
    assert main(["metrics"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("# HELP") and op.correlation()["run_id"] not in text
    assert main(["snapshot", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["complete"]


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"max_events": 1001}, {"max_seconds": 0}])
def test_bounds_are_explicit(kwargs):
    with pytest.raises(ValueError):
        monitor.snapshot(**kwargs)
