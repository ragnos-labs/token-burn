# SPDX-License-Identifier: Apache-2.0
"""Behavioral journal/export tests. HTTP is an ephemeral loopback fixture only."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from token_burn import operations
from token_burn.operations import Operation


@pytest.fixture(autouse=True)
def private_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_BURN_STATE_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("TOKEN_BURN_OTEL_ENABLED", "1")
    for name in (
        "OTEL_TRACES_EXPORTER",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HTTP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_COLLECTOR_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def collector(monkeypatch):
    records = []
    behavior = {"status": 200, "body": {}, "delay": 0, "content_type": "application/json"}
    entered = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            records.append({"path": self.path, "raw": raw, "headers": dict(self.headers)})
            entered.set()
            time.sleep(behavior["delay"])
            body = json.dumps(behavior["body"]).encode()
            self.send_response(behavior["status"])
            self.send_header("Content-Type", behavior["content_type"])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Location", "/redirected")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(operations, "_collector_endpoint", lambda: base)
    yield records, behavior, entered, base
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_start_is_durable_private_and_exportable_before_terminal(collector):
    records, _, _, _ = collector
    op = Operation.start(
        "evidence.capture",
        source_revision="a" * 40,
        source_ref="LOCAL-ONLY-PATH-and-command secret",
    )
    status = op.status()
    assert status["running"] and status["missing_terminal"]
    assert status["pending_signals"] == 2
    assert len(list((op.directory / "events").glob("*.json"))) == 1
    assert not records, "persistence does not silently dispatch network effects"
    for path in [op.directory, op.directory / "events", op.directory / "delivery"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in op.directory.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    result = op.export_pending()
    assert result["status"] == "accepted"
    assert result["missing_terminal"] and not result["backend_verified"]
    assert {r["path"] for r in records} == {"/v1/traces", "/v1/logs"}
    wire = b"".join(r["raw"] for r in records)
    assert b"LOCAL-ONLY" not in wire and b"owner_pid" not in wire
    assert b"source_ref" not in wire and b"owner_start_identity" not in wire
    assert b"token-burn" in wire
    traces = json.loads(records[0]["raw"])["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(traces) == 1
    assert traces[0]["kind"] == 1
    assert isinstance(traces[0]["startTimeUnixNano"], str)
    assert len(traces[0]["traceId"]) == 32 and len(traces[0]["spanId"]) == 16
    log = json.loads(records[1]["raw"])["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert log["traceId"] == op.correlation()["trace_id"]
    assert log["spanId"] == traces[0]["spanId"]
    assert json.loads(log["body"]["stringValue"])["lifecycle"] == "start"


def test_outage_retains_immutable_payloads_and_replay_accepts_once(collector):
    records, behavior, _, _ = collector
    behavior["status"] = 503
    op = Operation.start("cleanup.remove")
    op.finish("completed", evidence_ref="sha256:" + "b" * 64)
    before = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (op.directory / "events").glob("*.json")
    }
    result = op.export_pending()
    assert result["pending_signals"] == 4 and result["accepted_signals"] == 0
    assert result["pending_age_seconds"] >= 0
    assert result["error_types"] == ["HTTPError"]
    assert result["attempted_signals"] == 1
    first_payload = records[0]["raw"]
    behavior["status"] = 200
    assert Operation.open(op.correlation()["run_id"]).export_pending()["collector_accepted"]
    assert records[1]["raw"] == first_payload
    assert op.export_pending()["attempted_signals"] == 0
    assert before == {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (op.directory / "events").glob("*.json")
    }


def test_terminal_root_has_real_duration_and_distinct_stop_outcome(collector):
    records, _, _, _ = collector
    op = Operation.start("process.stop")
    time.sleep(0.01)
    receipt = op.finish("process_stopped", reason_code="process_signal", signal_number=15)
    assert receipt["outcome"] == "process_stopped"
    op.export_pending()
    terminal = json.loads(records[2]["raw"])["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(terminal) == 2
    root = terminal[1]
    assert int(root["endTimeUnixNano"]) - int(root["startTimeUnixNano"]) >= 10_000_000
    assert root["spanId"] == op.correlation()["root_span_id"]
    assert root["status"]["code"] == 1
    assert op.status()["state"] == "process_stopped"
    with pytest.raises(ValueError, match="operation_already_terminal"):
        op.stage("signal")


@pytest.mark.parametrize(
    "outcome,lifecycle", [("blocked", "block"), ("cancelled", "cancel"), ("failed", "end")]
)
def test_lifecycle_types_are_persisted_distinctly(outcome, lifecycle):
    op = Operation.start("cleanup.permit")
    op.finish(outcome, reason_code="interrupted" if outcome == "cancelled" else "admission_refused")
    assert op.read()["events"][-1]["lifecycle"] == lifecycle
    assert op.status()["terminal"]
    assert not op.status()["missing_terminal"]


def test_empty_partial_success_from_live_collector_is_full_acceptance(collector):
    _, behavior, _, _ = collector
    behavior["body"] = {"partialSuccess": {}}
    op = Operation.start("evidence.capture")
    assert op.export_pending()["collector_accepted"]
    assert not op.status()["backend_verified"]


def test_partial_success_is_held_not_retried_and_never_claimed_accepted(collector):
    records, behavior, _, _ = collector
    behavior["body"] = {"partialSuccess": {"rejectedSpans": "1", "errorMessage": "SECRET"}}
    op = Operation.start("evidence.cases")
    result = op.export_pending()
    assert result["held_signals"] == 1 and result["pending_signals"] == 1
    assert not result["collector_accepted"] and not result["backend_verified"]
    assert result["error_types"] == ["PartialSuccess"]
    assert b"SECRET" not in b"".join(p.read_bytes() for p in op.directory.rglob("*.json"))
    behavior["body"] = {}
    assert op.export_pending()["attempted_signals"] == 1
    assert op.export_pending()["attempted_signals"] == 0
    assert len(records) == 2


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 403])
def test_redirect_and_permanent_rejection_remain_held(collector, status):
    records, behavior, _, _ = collector
    behavior["status"] = status
    op = Operation.start("evidence.capture")
    result = op.export_pending()
    assert result["held_signals"] == 1 and result["pending_signals"] == 1
    assert result["attempted_signals"] == 1
    assert len(records) == 1
    assert all(r["path"] != "/redirected" for r in records)


def test_interrupted_acknowledgment_replays_same_ids(collector, monkeypatch):
    records, _, _, _ = collector
    op = Operation.start("cleanup.remove")
    send = operations._send

    def interrupted(*args):
        send(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(operations, "_send", interrupted)
    with pytest.raises(KeyboardInterrupt):
        op.export_pending()
    assert op.status()["pending_signals"] == 2
    monkeypatch.setattr(operations, "_send", send)
    assert op.export_pending()["collector_accepted"]
    assert records[0]["raw"] == records[1]["raw"]


def test_concurrent_replay_is_serialized(collector):
    records, behavior, entered, _ = collector
    behavior["delay"] = 0.1
    op = Operation.start("evidence.capture")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(op.export_pending)
        assert entered.wait(1)
        second = Operation.open(op.correlation()["run_id"]).export_pending()
        assert second["status"] == "busy"
        assert first.result()["collector_accepted"]
    assert len(records) == 2


def test_concurrent_stage_records_have_unique_sequences():
    op = Operation.start("evidence.capture")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: op.stage("running"), range(12)))
    events = op.read()["events"]
    assert [e["sequence"] for e in events] == list(range(13))
    assert len({e["event_id"] for e in events}) == 13


def test_request_timeout_and_budget_leave_pending(collector):
    _, behavior, _, _ = collector
    behavior["delay"] = 0.4
    op = Operation.start("evidence.capture")
    started = time.monotonic()
    result = op.export_pending(timeout=0.05, max_seconds=0.12)
    assert time.monotonic() - started < 0.5
    assert result["pending_signals"] == 2 and not result["collector_accepted"]


def test_actual_connection_outage_is_pending(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(operations, "_collector_endpoint", lambda: f"http://127.0.0.1:{port}")
    op = Operation.start("cleanup.remove")
    assert op.export_pending()["pending_signals"] == 2


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://cloud.langfuse.com/api/public/otel",
        "http://remote:4318",
        "http://user:secret@127.0.0.1:4318",
        "http://127.0.0.1:4318/?secret=value",
        "http://127.0.0.1:4318/#fragment",
        "http://127.0.0.1:4318/other",
        "http://127.0.0.1:4317/other",
    ],
)
def test_shared_resolver_cannot_expand_endpoint_authority(endpoint, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_COLLECTOR_ENDPOINT", "http://127.0.0.1:4318")
    op = Operation.start("evidence.capture")
    result = op.export_pending()
    assert result["status"] == "error"
    assert result["error_type"] == "ValueError"
    assert not result["collector_accepted"]


def test_shared_resolver_and_flags_reused(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4317/v1/traces")
    assert operations._collector_endpoint() == "http://127.0.0.1:4317"
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    op = Operation.start("evidence.capture")
    assert op.export_pending()["status"] == "disabled"
    assert op.status()["pending_signals"] == 2


def test_no_environment_headers_or_proxy_are_forwarded(collector, monkeypatch):
    records, _, _, _ = collector
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=SECRET")
    Operation.start("evidence.capture").export_pending()
    assert records
    assert all("Authorization" not in r["headers"] for r in records)


def test_validation_precedes_journal_creation_and_rejects_arbitrary_fields(tmp_path):
    with pytest.raises(ValueError, match="invalid_operation"):
        Operation.start("curl --token SECRET")
    assert not (tmp_path / "journal").exists()
    op = Operation.start("evidence.cases")
    for invoke in [
        lambda: op.stage("SECRET"),
        lambda: op.finish("failed", reason_code="SECRET"),
        lambda: op.finish("failed", evidence_ref="/private/secret.txt"),
    ]:
        with pytest.raises(ValueError):
            invoke()
    assert op.status()["event_count"] == 1


def test_journal_persistence_failure_does_not_return_operation(monkeypatch):
    original = operations._write

    def fail_start(path, value, **kwargs):
        if path.parent.name == "events":
            raise OSError("secret disk path")
        return original(path, value, **kwargs)

    monkeypatch.setattr(operations, "_write", fail_start)
    with pytest.raises(OSError):
        Operation.start("cleanup.remove")


def test_root_symlink_and_insecure_existing_directory_rejected(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("TOKEN_BURN_STATE_DIR", str(link))
    with pytest.raises((OSError, ValueError)):
        Operation.start("cleanup.remove")
    monkeypatch.setenv("TOKEN_BURN_STATE_DIR", str(actual))
    actual.chmod(0o755)
    with pytest.raises(ValueError, match="not_private"):
        Operation.start("cleanup.remove")


def test_owner_death_distinguished_from_running(monkeypatch):
    op = Operation.start("evidence.capture")
    monkeypatch.setattr(operations, "_process_identity", lambda _pid: (False, None))
    assert op.status()["state"] == "terminal_record_missing"
    assert not op.status()["running"]
    assert op.status()["missing_terminal"]
    op.finish("cancelled", reason_code="interrupted")
    assert op.status()["state"] == "cancelled"


def test_status_and_cli_read_are_bounded_and_do_not_export(collector):
    records, _, _, _ = collector
    op = Operation.start("evidence.capture", source_ref="LOCAL-SECRET")
    op.stage("spawn")
    page = op.read(limit=1)
    assert page["has_more"] and page["next_offset"] == 1
    result = subprocess.run(
        [sys.executable, "-m", "token_burn", "status", op.correlation()["run_id"]],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    status = json.loads(result.stdout)
    assert status["pending_signals"] == 4 and status["state"] == "running"
    assert "LOCAL-SECRET" not in result.stdout
    assert not records
