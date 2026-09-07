"""All tests use private temporary state and opt out of real telemetry."""

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_BURN_STATE_DIR", str(tmp_path / "operations"))
    monkeypatch.setenv("TOKEN_BURN_WORKTREE_STATE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("TOKEN_BURN_OTEL_ENABLED", "0")
    for name in (
        "TOKEN_BURN_OTLP_ENDPOINT",
        "OTEL_TRACES_EXPORTER",
        "OTEL_LOGS_EXPORTER",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HTTP_ENDPOINT",
        "OTEL_COLLECTOR_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
