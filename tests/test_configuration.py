"""Public configuration and installed-entrypoint privacy boundaries."""

import json
import subprocess
import sys

import pytest

from token_burn.config import collector_endpoint
from token_burn.operations import Operation


def test_export_is_off_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("TOKEN_BURN_OTEL_ENABLED", raising=False)
    operation = Operation.start("evidence.capture")
    result = operation.export_pending()
    assert result["status"] == "disabled"
    assert result["attempted_signals"] == 0
    assert operation.status()["pending_signals"] == 2


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://localhost:4318", "http://127.0.0.1:4318"),
        ("http://127.0.0.1:9999", "http://127.0.0.1:9999"),
        ("http://[::1]:4318/v1/traces", "http://[::1]:4318"),
    ],
)
def test_explicit_loopback_port_is_preserved(raw, expected, monkeypatch):
    monkeypatch.setenv("TOKEN_BURN_OTLP_ENDPOINT", raw)
    assert collector_endpoint() == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1:4318\n",
        "http://127.0.0.1:4318/../../private",
        "http://127.0.0.1:4318?secret=value",
        "http://user:password@127.0.0.1:4318",
        "https://127.0.0.1:4318",
        "http://remote.invalid:4318",
        "http://127.0.0.1:0",
        "http://localhost:65536",
        "http://localhost",
        "http://127.0.0.1:4318#fragment",
    ],
)
def test_invalid_routes_are_rejected_before_network(raw, monkeypatch):
    monkeypatch.setenv("TOKEN_BURN_OTLP_ENDPOINT", raw)
    with pytest.raises(ValueError):
        collector_endpoint()


@pytest.mark.parametrize("command", ["status", "worktree", "capture"])
def test_cli_errors_do_not_echo_large_or_sensitive_input(command, tmp_path):
    marker = "PRIVATE-ARGUMENT-" + "x" * 20_000
    result = subprocess.run(
        [sys.executable, "-m", "token_burn", command, "--" + marker],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert marker not in result.stdout + result.stderr
    assert len(result.stdout + result.stderr) < 512


def test_installed_entrypoint_runs_capture_and_cursor_read_from_foreign_cwd(tmp_path):
    directory = tmp_path / "evidence"
    captured = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_burn",
            "capture",
            "--output-dir",
            str(directory),
            "--",
            sys.executable,
            "-c",
            "import sys;print('retained');sys.exit(7)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert captured.returncode == 7
    receipt = json.loads((directory / "receipt.json").read_text())
    status = subprocess.run(
        [sys.executable, "-m", "token_burn", "status", receipt["operation"]["run_id"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(status.stdout)["state"] == "failed"
    page = subprocess.run(
        [
            sys.executable,
            "-m",
            "token_burn",
            "read",
            str(directory / "stdout.log"),
            "--limit-bytes",
            "4",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(page.stdout)["text"] == "reta"
    assert json.loads(page.stdout)["next_offset"] == 4
