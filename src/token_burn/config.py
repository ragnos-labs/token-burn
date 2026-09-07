# SPDX-License-Identifier: Apache-2.0
"""Explicit, local-only telemetry configuration. No SDK or ambient credentials."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


def otel_export_disabled() -> bool:
    """Network export is opt-in; standard exporter disable flags still win."""
    if os.getenv("TOKEN_BURN_OTEL_ENABLED", "").lower() not in {"1", "true", "yes"}:
        return True
    return any(
        os.getenv(name, "").lower() == "none"
        for name in ("OTEL_TRACES_EXPORTER", "OTEL_LOGS_EXPORTER")
    )


def collector_endpoint() -> str:
    """Return a literal loopback OTLP/HTTP base, refusing proxies and remote hosts.

    The collector owns remote backend authentication and delivery. This client
    never loads headers, tokens, system proxy settings, or redirect destinations.
    """
    raw = next(
        (
            os.environ[name]
            for name in (
                "TOKEN_BURN_OTLP_ENDPOINT",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "OTEL_EXPORTER_OTLP_HTTP_ENDPOINT",
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "OTEL_COLLECTOR_ENDPOINT",
            )
            if os.environ.get(name)
        ),
        "http://127.0.0.1:4318",
    )
    if any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        raise ValueError("collector_route_rejected")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
        or parsed.path not in {"", "/", "/v1/traces"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("collector_route_rejected")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{parsed.port}"
