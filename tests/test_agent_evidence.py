# SPDX-License-Identifier: Apache-2.0
"""Behavioral checks for retained evidence and bounded model-facing output."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/token_burn/evidence.py"
SPEC = importlib.util.spec_from_file_location("agent_evidence", SCRIPT)
assert SPEC and SPEC.loader
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)


@pytest.fixture(autouse=True)
def isolated_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_BURN_STATE_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("TOKEN_BURN_OTEL_ENABLED", "0")


def operation_events(value: dict) -> list[dict]:
    operation = EVIDENCE.Operation.open(value["operation"]["run_id"])
    return operation.read()["events"]


def cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )


def test_capture_preserves_large_streams_failure_and_runs_once(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    counter = tmp_path / "runs"
    program = (
        "import os,pathlib,sys; "
        f"p=pathlib.Path({str(counter)!r});p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "os.write(1,b'PASS\\n'*100000);os.write(2,b'buried failure\\x00\\xff');sys.exit(7)"
    )
    result = cli("capture", "--output-dir", str(directory), "--", sys.executable, "-c", program)
    assert result.returncode == 7
    value = json.loads(result.stdout)
    assert len(result.stdout.encode()) <= EVIDENCE.DEFAULT_BUDGET
    assert value["exit_code"] == 7
    assert value["test_counts"] is None
    assert (directory / "stdout.log").read_bytes() == b"PASS\n" * 100000
    assert (directory / "stderr.log").read_bytes() == b"buried failure\x00\xff"
    assert value["stdout"]["lines"] == 100000
    assert counter.read_text() == "x"
    page = cli("read", str(directory / "stderr.log"))
    assert page.returncode == 0
    assert "buried failure" in json.loads(page.stdout)["text"]
    assert counter.read_text() == "x"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.iterdir())


def test_capture_waits_for_descendant_pipe_eof_before_receipt(tmp_path: Path) -> None:
    child = "import time;time.sleep(0.2);print('late evidence',flush=True)"
    parent = (
        "import subprocess,sys;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
        "print('parent evidence',flush=True)"
    )
    result = cli(
        "capture", "--output-dir", str(tmp_path / "run"), "--", sys.executable, "-c", parent
    )
    assert result.returncode == 0
    value = json.loads(result.stdout)
    assert value["output_complete"] is True
    assert (tmp_path / "run/stdout.log").read_bytes() == b"parent evidence\nlate evidence\n"
    assert value["stdout"] == EVIDENCE._facts(tmp_path / "run/stdout.log")


def test_inherited_writer_beyond_grace_is_explicitly_incomplete_and_cannot_mutate_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(EVIDENCE, "POST_EXIT_DRAIN_SECONDS", 0.03)
    child = "import time,os;time.sleep(0.3)\ntry: os.write(1,b'late')\nexcept BrokenPipeError: pass"
    parent = (
        "import subprocess,sys;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
        "print('parent evidence',flush=True)"
    )
    value = EVIDENCE.capture([sys.executable, "-c", parent], tmp_path / "run")
    assert value["status"] == "output_incomplete"
    assert value["exit_code"] == 0
    assert value["output_complete"] is False
    before = (tmp_path / "run/stdout.log").read_bytes()
    time.sleep(0.4)
    assert (tmp_path / "run/stdout.log").read_bytes() == before
    assert value["stdout"] == EVIDENCE._facts(tmp_path / "run/stdout.log")


def test_cases_keep_buried_failures_and_complete_records(tmp_path: Path) -> None:
    records = [{"case": f"case-{i}", "pass": i != 399, "detail": "x" * 300} for i in range(400)]
    path = tmp_path / "input.json"
    path.write_text(json.dumps(records))
    result = cli("cases", "--input", str(path), "--output-dir", str(tmp_path / "cases"))
    assert result.returncode == 1
    value = json.loads(result.stdout)
    assert (value["total"], value["passed"], value["failed"]) == (400, 399, 1)
    assert value["failure_preview"] == [
        {"case_line": 400, "case": "case-399", "name_shortened": False}
    ]
    assert value["failure_preview_complete"] is True
    retained = (tmp_path / "cases/cases.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in retained] == records
    failures = [
        json.loads(line) for line in (tmp_path / "cases/failures.jsonl").read_text().splitlines()
    ]
    assert failures == [{"case_line": 400, "result": records[-1]}]
    assert len(result.stdout.encode()) <= EVIDENCE.DEFAULT_BUDGET


def test_summary_bounds_long_failure_names_without_losing_failure_count(tmp_path: Path) -> None:
    records = [{"case": "\U0001f333" * 5000 + str(i), "pass": False} for i in range(35)]
    receipt = EVIDENCE.write_cases(records, tmp_path / "report")
    text = EVIDENCE.summary(receipt, 1400)
    value = json.loads(text)
    assert len((text + "\n").encode()) <= 1400
    assert value["failed"] == 35
    assert not value["failure_preview_complete"]
    assert (tmp_path / "report/failures.jsonl").read_text().count("\n") == 35
    assert len(receipt["failure_preview"]) == 10


def test_read_pages_progress_through_long_utf8_line(tmp_path: Path) -> None:
    original = "ab\U0001f333\u00e9" * 9000
    path = tmp_path / "long.log"
    path.write_text(original, encoding="utf-8")
    offset = 0
    parts = []
    while True:
        value = EVIDENCE.read_page(path, offset, 17)
        parts.append(value["text"])
        assert value["next_offset"] > offset
        offset = value["next_offset"]
        if value["complete"]:
            break
    assert "".join(parts) == original
    assert offset == len(original.encode())


def test_read_bounds_serialized_escaping_and_preserves_progress(tmp_path: Path) -> None:
    path = tmp_path / "control.log"
    path.write_bytes(b"\x00" * 12000)
    result = cli("read", str(path), "--limit-bytes", "12000", "--summary-bytes", "1000")
    assert result.returncode == 0
    value = json.loads(result.stdout)
    assert len(result.stdout.encode()) <= 1000
    assert 0 < value["next_offset"] < 12000
    assert not value["complete"]


@pytest.mark.parametrize(
    "records",
    [
        [],
        {},
        [{"case": "x", "pass": 1}],
        [{"case": "x", "pass": "false"}],
        [{"case": " ", "pass": True}],
        [{"case": "x", "pass": True, "value": float("nan")}],
        [{"case": "\ud800", "pass": True}],
        [{"case": "x", "pass": True, "nested": [{"\udcff": "value"}]}],
    ],
)
def test_malformed_case_evidence_never_becomes_green(tmp_path: Path, records: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        EVIDENCE.write_cases(records, tmp_path / "report")
    assert not (tmp_path / "report").exists()


def test_existing_directory_is_preserved_without_command_execution(tmp_path: Path) -> None:
    directory = tmp_path / "existing"
    directory.mkdir()
    marker = directory / "marker"
    marker.write_text("preserve")
    result = cli(
        "capture",
        "--output-dir",
        str(directory),
        "--",
        sys.executable,
        "-c",
        "raise Exception('should not execute')",
    )
    assert result.returncode == 2
    assert marker.read_text() == "preserve"
    assert list(directory.iterdir()) == [marker]


def test_command_start_failure_retains_an_explicit_non_success_receipt(tmp_path: Path) -> None:
    result = cli(
        "capture", "--output-dir", str(tmp_path / "run"), "--", str(tmp_path / "missing-command")
    )
    assert result.returncode == 2
    value = json.loads(result.stdout)
    assert value["status"] == "command_start_failed"
    assert value["exit_code"] is None
    assert json.loads((tmp_path / "run/receipt.json").read_text()) == value


def test_invalid_budget_rejects_before_execution(tmp_path: Path) -> None:
    result = cli(
        "capture",
        "--output-dir",
        str(tmp_path / "run"),
        "--summary-bytes",
        "0",
        "--",
        sys.executable,
        "-c",
        "print('bad')",
    )
    assert result.returncode == 2
    assert not (tmp_path / "run").exists()


def test_unknown_arguments_have_bounded_non_reflecting_errors(tmp_path: Path) -> None:
    marker = "private-argument-" + "x" * 20000
    result = cli("read", str(tmp_path / "unused"), "--summary-bytes", "512", "--unknown=" + marker)
    assert result.returncode == 2
    assert len((result.stdout + result.stderr).encode()) <= 512
    assert marker not in result.stderr
    assert json.loads(result.stderr)["status"] == "evidence_error"


def test_deep_json_resource_rejection_is_bounded_without_partial_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "deep.json"
    source.write_text(
        '[{"case":"nested","pass":true,"diagnostic":' + "[" * 30000 + "0" + "]" * 30000 + "}]"
    )
    result = cli(
        "cases",
        "--input",
        str(source),
        "--output-dir",
        str(tmp_path / "run"),
        "--summary-bytes",
        "512",
    )
    assert result.returncode == 2
    assert len((result.stdout + result.stderr).encode()) <= 512
    assert json.loads(result.stderr)["error_type"] == "RecursionError"
    assert not (tmp_path / "run").exists()


def test_help_is_bounded_at_the_smallest_supported_budget(tmp_path: Path) -> None:
    result = cli("read", str(tmp_path / "unused"), "--summary-bytes", "512", "--help")
    assert result.returncode == 0
    assert len((result.stdout + result.stderr).encode()) <= 512
    assert json.loads(result.stdout)["status"] == "help"


@pytest.mark.parametrize(
    "command",
    [
        ["invalid\x00argv"],
        [sys.executable, b"bytes"],
        "not-an-argv-array",
        [],
        ["invalid\ud800argv"],
        ["invalid\udc00argv"],
    ],
)
def test_invalid_api_argv_rejects_before_artifact_creation(tmp_path: Path, command: object) -> None:
    with pytest.raises(ValueError):
        EVIDENCE.capture(command, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_valid_filesystem_surrogate_escape_argument_remains_usable(tmp_path: Path) -> None:
    value = EVIDENCE.capture([sys.executable, "-c", "print('ok')", "\udcff"], tmp_path / "run")
    assert value["status"] == "completed"
    assert value["exit_code"] == 0
    assert (tmp_path / "run/stdout.log").read_bytes() == b"ok\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX collector signal and process-group contract")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize(
    "lifecycle", ["direct", "descendant", "exited_parent", "closed_pipes", "escaped_descendant"]
)
def test_collector_cancellation_retains_receipt_and_stops_process_group(
    tmp_path: Path, signum: int, lifecycle: str
) -> None:
    directory = tmp_path / "capture"
    ready = tmp_path / "ready"
    pid_file = tmp_path / "pids"
    worker = (
        "import os,pathlib,signal,time;"
        "signal.signal(signal.SIGINT,signal.SIG_IGN);"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(pid_file)!r}).open('a').write(str(os.getpid())+'\\n');"
        "os.write(1,b'before cancellation\\x00\\xff\\n'*1000);"
        "os.write(2,b'error evidence\\n'*1000);"
        f"pathlib.Path({str(ready)!r}).touch();"
        "time.sleep(30)"
    )
    if lifecycle == "closed_pipes":
        worker = worker.replace("time.sleep(30)", "os.close(1);os.close(2);time.sleep(30)")
    if lifecycle == "escaped_descendant":
        worker = "import os;os.setsid();" + worker
    if lifecycle in ("descendant", "exited_parent", "escaped_descendant"):
        worker = (
            "import os,pathlib,subprocess,sys,time;"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())+'\\n');"
            f"subprocess.Popen([sys.executable,'-c',{worker!r}]);time.sleep(30)"
        )
    if lifecycle == "exited_parent":
        worker = worker.removesuffix("time.sleep(30)") + "sys.exit(0)"
    collector = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "capture",
            "--output-dir",
            str(directory),
            "--",
            sys.executable,
            "-c",
            worker,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pids = []
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if ready.exists() and all(
                (directory / f"{name}.log").stat().st_size > 0 for name in ("stdout", "stderr")
            ):
                break
            assert collector.poll() is None
            time.sleep(0.01)
        assert ready.exists()
        pids = [int(pid) for pid in pid_file.read_text().splitlines()]
        # Signal only the collector, not a terminal/process group.
        os.kill(collector.pid, signum)
        out, err = collector.communicate(timeout=5)
        assert collector.returncode == 128 + signum, (out, err)
        receipt = json.loads((directory / "receipt.json").read_text())
        assert receipt == json.loads(out)
        assert receipt["status"] == "interrupted"
        assert receipt["interruption_signal"] == signum
        assert receipt["termination_scope"] == "process_group"
        assert receipt["output_complete"] is False
        assert receipt["direct_child_reaped"] is True
        assert operation_events(receipt)[-1]["outcome"] == "cancelled"
        for name in ("stdout", "stderr"):
            raw = (directory / f"{name}.log").read_bytes()
            assert raw
            assert receipt[name]["bytes"] == len(raw)
            assert receipt[name]["sha256"] == hashlib.sha256(raw).hexdigest()
        for pid in pids:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
            ).stdout.strip()
            if lifecycle == "escaped_descendant" and pid == pids[-1]:
                assert state and not state.startswith("Z"), (pid, state)
            else:
                assert not state or state.startswith("Z"), (pid, state)
    finally:
        if collector.poll() is None:
            collector.kill()
            collector.communicate(timeout=5)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("transport", ["stdin", "file"])
@pytest.mark.parametrize(
    "payload",
    [
        b'[{"case":"bad\xff","pass":true}]',
        b'[{"case":"bad","pass":true,"detail":"\xed\xa0\x80"}]',
        b'[{"case":"bad\\ud800","pass":true}]',
        b'[{"case":"bad","pass":true,"nested":{"\\udcff":1}}]',
    ],
)
def test_cases_reject_invalid_unicode_before_artifacts(
    tmp_path: Path, transport: str, payload: bytes
) -> None:
    source = tmp_path / "input.json"
    source.write_bytes(payload)
    directory = tmp_path / "cases"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "cases",
            "--input",
            "-" if transport == "stdin" else str(source),
            "--output-dir",
            str(directory),
        ],
        input=payload if transport == "stdin" else None,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["status"] == "evidence_error"
    assert not directory.exists()


@pytest.mark.parametrize("transport", ["stdin", "file"])
@pytest.mark.parametrize("escaped", [False, True])
def test_cases_preserve_valid_non_ascii_on_both_transports(
    tmp_path: Path, transport: str, escaped: bool
) -> None:
    records = [{"case": "caf\u00e9 \U0001f333", "pass": True, "detail": ["\u65e5\u672c"]}]
    payload = json.dumps(records, ensure_ascii=escaped).encode("utf-8")
    source = tmp_path / "input.json"
    source.write_bytes(payload)
    directory = tmp_path / "cases"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "cases",
            "--input",
            "-" if transport == "stdin" else str(source),
            "--output-dir",
            str(directory),
        ],
        input=payload if transport == "stdin" else None,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert [
        json.loads(line) for line in (directory / "cases.jsonl").read_text().splitlines()
    ] == records


def test_capture_restores_handlers_and_remains_usable_from_worker_threads(tmp_path: Path) -> None:
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    result = EVIDENCE.capture([sys.executable, "-c", "print('main')"], tmp_path / "main")
    assert result["status"] == "completed"
    assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            EVIDENCE.capture, [sys.executable, "-c", "print('worker')"], tmp_path / "worker"
        ).result(timeout=5)
    assert result["status"] == "completed"


def test_cancellation_during_hashing_is_reflected_in_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = EVIDENCE._facts

    def interrupt(path: Path) -> dict:
        value = original(path)
        os.kill(os.getpid(), signal.SIGINT)
        return value

    monkeypatch.setattr(EVIDENCE, "_facts", interrupt)
    value = EVIDENCE.capture([sys.executable, "-c", "print('finished')"], tmp_path / "run")
    assert value["status"] == "interrupted"
    assert value["interruption_signal"] == signal.SIGINT
    assert value["output_complete"] is False
    assert value["direct_child_reaped"] is True
    assert value["exit_code"] == 0
    assert json.loads((tmp_path / "run/receipt.json").read_text()) == value


def test_capture_journals_before_spawn_and_correlates_private_retained_evidence(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private-capture"
    secret_marker = "private-argv-and-output-marker"
    program = (
        "import json,os,pathlib,sys;"
        "root=pathlib.Path(os.environ['TOKEN_BURN_STATE_DIR']);"
        "events=[json.loads(p.read_text())['event'] for p in root.glob('*/events/*.json')];"
        "assert any(e['lifecycle']=='start' for e in events);"
        "assert any(e['stage']=='spawn' for e in events);"
        f"print({secret_marker!r});sys.exit(7)"
    )
    result = cli("capture", "--output-dir", str(directory), "--", sys.executable, "-c", program)
    assert result.returncode == 7
    value = json.loads(result.stdout)
    correlation = value["operation"]
    assert len(correlation["run_id"]) == len(correlation["trace_id"]) == 32
    assert len(correlation["root_span_id"]) == 16
    assert all(int(correlation[key], 16) for key in ("run_id", "trace_id", "root_span_id"))
    manifest = json.loads(Path(correlation["receipt"]).read_text())
    assert manifest["source_ref"] == str(directory / "receipt.json")
    events = operation_events(value)
    assert [event["stage"] for event in events if event["lifecycle"] == "stage"] == [
        "validate",
        "spawn",
        "drain",
        "hash",
        "receipt",
    ]
    assert events[-1]["outcome"] == "failed"
    assert events[-1]["exit_code"] == 7
    assert (
        events[-1]["evidence_ref"]
        == hashlib.sha256((directory / "receipt.json").read_bytes()).hexdigest()
    )
    envelope_text = "".join(
        path.read_text() for path in Path(correlation["receipt"]).parent.glob("events/*.json")
    )
    assert secret_marker not in envelope_text
    assert str(tmp_path) not in envelope_text
    status = json.loads((directory / correlation["status_file"]).read_text())
    assert status["terminal_recorded"] is True
    assert status["export"]["status"] == "disabled"
    assert status["export"]["backend_verified"] is False


def test_journal_start_outage_blocks_child_and_case_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        raise OSError("private journal failure")

    monkeypatch.setattr(EVIDENCE.Operation, "start", fail)
    marker = tmp_path / "ran"
    with pytest.raises(OSError):
        EVIDENCE.capture(
            [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"],
            tmp_path / "capture",
        )
    with pytest.raises(OSError):
        EVIDENCE.write_cases([{"case": "valid", "pass": True}], tmp_path / "cases")
    assert not marker.exists()
    assert not (tmp_path / "capture").exists()
    assert not (tmp_path / "cases").exists()


def test_required_spawn_stage_outage_stops_before_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = EVIDENCE.Operation.stage

    def fail_spawn(operation, name, **kwargs):
        if name == "spawn":
            raise OSError("private stage failure")
        return original(operation, name, **kwargs)

    monkeypatch.setattr(EVIDENCE.Operation, "stage", fail_spawn)
    with pytest.raises(OSError):
        EVIDENCE.capture(
            [sys.executable, "-c", "raise Exception('must not run')"], tmp_path / "capture"
        )
    assert not (tmp_path / "capture").exists()
    events = [
        json.loads(p.read_text())["event"] for p in (tmp_path / "journal").glob("*/events/*.json")
    ]
    assert any(e["lifecycle"] == "block" for e in events)


def test_terminal_journal_failure_preserves_original_evidence_and_reports_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_finish(*args, **kwargs):
        raise OSError("private terminal failure")

    monkeypatch.setattr(EVIDENCE.Operation, "finish", fail_finish)
    value = EVIDENCE.capture([sys.executable, "-c", "print('retained')"], tmp_path / "capture")
    assert value["status"] == "completed"
    assert (tmp_path / "capture/stdout.log").read_bytes() == b"retained\n"
    assert json.loads((tmp_path / "capture/receipt.json").read_text()) == value
    status = json.loads((tmp_path / "capture/operation-status.json").read_text())
    assert status["terminal_recorded"] is False
    assert status["error_type"] == "OSError"
    assert "private terminal failure" not in json.dumps(status)
    assert EVIDENCE.Operation.open(value["operation"]["run_id"]).status()["missing_terminal"]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_cancellation_received_during_start_export_never_spawns_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: int
) -> None:
    def interrupt_export(*args, **kwargs):
        os.kill(os.getpid(), signum)
        return {"status": "disabled"}

    monkeypatch.setattr(EVIDENCE.Operation, "export_pending", interrupt_export)
    marker = tmp_path / "ran"
    value = EVIDENCE.capture(
        [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"],
        tmp_path / "capture",
    )
    assert not marker.exists()
    assert value["status"] == "interrupted"
    assert value["interruption_signal"] == signum
    assert value["stdout"]["bytes"] == 0
    assert operation_events(value)[-1]["outcome"] == "cancelled"


def test_absolute_cli_works_from_foreign_cwd_and_read_has_no_operation_side_effects(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "capture",
            "--output-dir",
            str(tmp_path / "capture"),
            "--",
            sys.executable,
            "-c",
            "print('foreign cwd')",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    before = sorted(str(p) for p in (tmp_path / "journal").rglob("*"))
    result = cli("read", str(tmp_path / "capture/stdout.log"))
    assert result.returncode == 0
    assert sorted(str(p) for p in (tmp_path / "journal").rglob("*")) == before


def test_cases_journal_literal_counts_and_unicode_admission_block(tmp_path: Path) -> None:
    records = [{"case": "secret-case-name", "pass": False, "detail": "private payload"}]
    value = EVIDENCE.write_cases(records, tmp_path / "valid")
    assert value["failed"] == 1
    events = operation_events(value)
    assert events[-1]["outcome"] == "failed"
    assert "secret-case-name" not in json.dumps(events)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "cases",
            "--input",
            "-",
            "--output-dir",
            str(tmp_path / "invalid"),
        ],
        input=b'[{"case":"invalid\xff","pass":true}]',
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    error = json.loads(result.stderr)
    assert error["error_type"] == "UnicodeDecodeError"
    assert operation_events(error)[-1]["outcome"] == "blocked"
    assert not (tmp_path / "invalid").exists()


def test_dirty_source_never_claims_head_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        EVIDENCE.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, b" M src/token_burn/evidence.py\n", b""
        ),
    )
    assert EVIDENCE._source_revision() is None


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_cases_waiting_for_stdin_can_cancel_with_a_local_terminal_record(
    tmp_path: Path, signum: int
) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "cases",
            "--input",
            "-",
            "--output-dir",
            str(tmp_path / "cases"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = [
                json.loads(p.read_text())["event"]
                for p in (tmp_path / "journal").glob("*/events/*.json")
            ]
            if any(event["stage"] == "validate" for event in events):
                break
            assert process.poll() is None
            time.sleep(0.01)
        assert any(event["stage"] == "validate" for event in events)
        os.kill(process.pid, signum)
        # wait() leaves stdin open, proving cancellation unblocks the read.
        process.wait(timeout=5)
        out, err = process.communicate(timeout=1)
        assert process.returncode == 128 + signum, (out, err)
        value = json.loads(err)
        assert value["status"] == "interrupted"
        terminal = operation_events(value)[-1]
        assert terminal["outcome"] == "cancelled"
        assert terminal["signal_number"] == signum
        assert not (tmp_path / "cases").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_export_exception_does_not_change_command_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_export(*args, **kwargs):
        raise RuntimeError("private exporter details")

    monkeypatch.setattr(EVIDENCE.Operation, "export_pending", fail_export)
    value = EVIDENCE.capture([sys.executable, "-c", "raise SystemExit(9)"], tmp_path / "capture")
    assert value["exit_code"] == 9
    assert operation_events(value)[-1]["outcome"] == "failed"
    status = json.loads((tmp_path / "capture/operation-status.json").read_text())
    assert status["terminal_recorded"]
    assert status["export"]["error_type"] == "RuntimeError"
    assert status["export"]["backend_verified"] is False
    assert "private exporter details" not in json.dumps(status)


def test_clean_source_head_is_recorded_when_git_proves_it(monkeypatch: pytest.MonkeyPatch) -> None:
    revision = "a" * 40
    results = iter(
        [
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, revision.encode() + b"\n", b""),
        ]
    )
    monkeypatch.setattr(EVIDENCE.subprocess, "run", lambda *args, **kwargs: next(results))
    assert EVIDENCE._source_revision() == revision


def test_unreaped_cancel_is_actionable_in_operation_history(tmp_path: Path) -> None:
    directory = tmp_path / "unreaped"
    directory.mkdir()
    run = EVIDENCE._EvidenceRun("evidence.capture", directory)
    value = run.receipt(
        directory,
        {
            "kind": "command",
            "status": "interrupted",
            "exit_code": None,
            "interruption_signal": int(signal.SIGTERM),
            "direct_child_reaped": False,
        },
    )
    terminal = operation_events(value)[-1]
    assert terminal["outcome"] == "cancelled"
    assert terminal["reason_code"] == "child_not_reaped"
    assert terminal["signal_number"] == signal.SIGTERM


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signalled_direct_child_does_not_leave_its_group_running(
    tmp_path: Path, signum: int
) -> None:
    child_pid_file = tmp_path / "direct.pid"
    descendant_pid_file = tmp_path / "descendant.pid"
    descendant = (
        "import os,pathlib,signal,time;"
        "signal.signal(signal.SIGINT,signal.SIG_IGN);"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(descendant_pid_file)!r}).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )
    command = (
        "import os,pathlib,subprocess,sys,time;"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid()));"
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}]);time.sleep(30)"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "capture",
            "--output-dir",
            str(tmp_path / "capture"),
            "--",
            sys.executable,
            "-c",
            command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pids = []
    try:
        deadline = time.monotonic() + 5
        while not descendant_pid_file.exists():
            assert time.monotonic() < deadline and process.poll() is None
            time.sleep(0.01)
        pids = [int(p.read_text()) for p in (child_pid_file, descendant_pid_file)]
        os.kill(pids[0], signum)
        out, err = process.communicate(timeout=5)
        value = json.loads(out)
        assert process.returncode == 2, (out, err)
        assert value["exit_code"] == -signum
        assert value["status"] == "output_incomplete" and not value["output_complete"]
        assert value["direct_child_reaped"] is True
        for pid in pids:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
            ).stdout.strip()
            assert not state or state.startswith("Z"), (pid, state)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
