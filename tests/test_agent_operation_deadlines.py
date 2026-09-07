# SPDX-License-Identifier: Apache-2.0
"""A byte-producing receiver cannot turn an inline export into an unbounded wait."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from token_burn import operations
from token_burn.operations import Operation


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_response_dribble_obeys_total_transport_deadline(tmp_path, monkeypatch, phase):
    monkeypatch.setenv("TOKEN_BURN_STATE_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("TOKEN_BURN_OTEL_ENABLED", "1")
    monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    entered = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            body = b"{}" + b" " * 18
            headers = (
                b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 20\r\n\r\n"
            )
            try:
                if phase == "body":
                    self.connection.sendall(headers)
                data = body if phase == "body" else headers + body
                for byte in data:
                    self.connection.sendall(bytes([byte]))
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    monkeypatch.setattr(
        operations, "_collector_endpoint", lambda: f"http://127.0.0.1:{server.server_port}"
    )
    try:
        op = Operation.start("evidence.capture")
        started = time.monotonic()
        result = op.export_pending(timeout=0.1, max_seconds=0.2)
        elapsed = time.monotonic() - started
        assert entered.is_set()
        assert elapsed < 0.45, elapsed
        assert result["pending_signals"] == 2 and result["accepted_signals"] == 0
        assert result["held_signals"] == 0
        assert not result["collector_accepted"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
