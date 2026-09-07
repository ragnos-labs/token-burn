# SPDX-License-Identifier: Apache-2.0
#!/usr/bin/env python3
"""Retain offline command/case evidence before emitting a bounded JSON summary."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, NoReturn

ROOT = Path(__file__).resolve().parents[2]
from token_burn.operations import Operation  # noqa: E402

DEFAULT_BUDGET = 6000
MAX_READ_BYTES = 12000
POST_EXIT_DRAIN_SECONDS = 1.0
CANCEL_GRACE_SECONDS = 0.2
KILL_WAIT_SECONDS = 0.5


class _HelpRequested(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """Do not let argparse reflect arbitrary argv outside the response budget."""

    def error(self, message: str) -> NoReturn:
        raise ValueError("invalid_invocation")

    def print_help(self, file: Any = None) -> None:
        # The caller renders one compact help response, including for subcommands.
        return None

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if status == 0:
            raise _HelpRequested
        raise ValueError("invalid_invocation")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _directory(path: Path) -> Path:
    path = path.absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


def _facts(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = newlines = 0
    last = b""
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
            size += len(chunk)
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
    return {
        "file": path.name,
        "bytes": size,
        "lines": newlines + int(bool(size) and last != b"\n"),
        "sha256": digest.hexdigest(),
    }


def _receipt(directory: Path, value: dict[str, Any]) -> dict[str, Any]:
    _write(directory / "receipt.json", (_json(value) + "\n").encode())
    return value


def _source_revision() -> str | None:
    """Only label clean owning source; a dirty checkout is not its HEAD bytes."""
    if not (ROOT / "src" / "token_burn" / "evidence.py").is_file():
        return None
    try:
        state = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True,
            timeout=0.5,
            check=False,
        )
        if state.returncode or state.stdout:
            return None
        head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            timeout=0.5,
            check=False,
        )
        revision = head.stdout.decode("ascii").strip()
        return revision if head.returncode == 0 else None
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


class _EvidenceRun:
    """Keep journal failures separate from retained command/case evidence."""

    def __init__(self, name: str, directory: Path) -> None:
        # The source reference stays in the private local manifest only.
        self.operation = Operation.start(
            name,
            source_revision=_source_revision(),
            source_ref=str((directory / "receipt.json").absolute()),
        )
        self.mutating = False
        self.finished = False
        self.error_type: str | None = None

    def correlation(self) -> dict[str, str]:
        return {
            key: value
            for key, value in self.operation.correlation().items()
            if key in {"run_id", "trace_id", "root_span_id"}
        }

    def export(self) -> dict[str, Any]:
        try:
            delivery = self.operation.export_pending(max_seconds=0.5)
            return {
                key: delivery.get(key)
                for key in (
                    "status",
                    "collector_accepted",
                    "backend_verified",
                    "pending_signals",
                    "held_signals",
                    "error_type",
                )
            }
        except Exception as exc:  # noqa: BLE001 - optional export must preserve local evidence
            return {
                "status": "pending",
                "error_type": type(exc).__name__,
                "collector_accepted": False,
                "backend_verified": False,
            }

    def stage(self, name: str, *, required: bool = False) -> None:
        try:
            self.operation.stage(name)
        except Exception as exc:
            self.error_type = type(exc).__name__
            if required:
                raise

    def finish(self, outcome: str, **fields: Any) -> dict[str, Any]:
        try:
            terminal = self.operation.finish(outcome, **fields)
            self.finished = True
            result = {"terminal_recorded": True, "receipt": terminal["receipt"]}
        except Exception as exc:  # noqa: BLE001 - retain evidence and report a typed journal gap
            self.error_type = type(exc).__name__
            result = {"terminal_recorded": False, "error_type": self.error_type}
        result["export"] = self.export()
        if self.error_type:
            result["journal_error_type"] = self.error_type
        return result

    def receipt(self, directory: Path, value: dict[str, Any]) -> dict[str, Any]:
        self.stage("receipt")
        value["operation"] = {
            **self.correlation(),
            "receipt": str(self.operation.directory / "run.json"),
            "status_file": "operation-status.json",
        }
        _receipt(directory, value)
        if value["status"] == "interrupted":
            outcome = "cancelled"
            reason = (
                "child_not_reaped" if value.get("direct_child_reaped") is False else "interrupted"
            )
        elif value["status"] == "command_start_failed":
            outcome, reason = "blocked", "internal_error"
        elif value["kind"] == "cases":
            outcome, reason = ("failed" if value["failed"] else "completed"), "completed"
        elif value["status"] != "completed":
            outcome = "failed"
            reason = (
                "process_signal"
                if isinstance(value.get("exit_code"), int) and value["exit_code"] < 0
                else "internal_error"
            )
        else:
            outcome = "failed" if value["exit_code"] else "completed"
            reason = "process_signal" if value["exit_code"] < 0 else "process_exit"
        status = self.finish(
            outcome,
            reason_code=reason,
            evidence_ref=hashlib.sha256((directory / "receipt.json").read_bytes()).hexdigest(),
            exit_code=value.get("exit_code"),
            signal_number=value.get("interruption_signal"),
        )
        try:
            _write(directory / "operation-status.json", (_json(status) + "\n").encode())
        except OSError:
            # The original evidence receipt is already durable. A missing status
            # sidecar or terminal event is an explicit gap, never success proof.
            pass
        return value


@contextmanager
def _operation(name: str, directory: Path) -> Iterator[_EvidenceRun]:
    run = _EvidenceRun(name, directory)
    try:
        run.export()
        run.stage("validate", required=True)
        yield run
    except KeyboardInterrupt as exc:
        if not run.finished:
            run.finish(
                "cancelled",
                reason_code="interrupted",
                signal_number=getattr(exc, "interruption_signal", signal.SIGINT),
            )
        exc.operation = run.correlation()
        raise
    except Exception as exc:
        if not run.finished:
            run.finish(
                "failed" if run.mutating else "blocked",
                reason_code="internal_error" if run.mutating else "validation_failed",
            )
        exc.operation = run.correlation()
        raise


@contextmanager
def _cancellation(*, defer: bool = True) -> Iterator[list[int]]:
    """Defer catchable signals until the collector can retain its receipt."""
    received: list[int] = []
    if threading.current_thread() is not threading.main_thread():
        # Only the main thread may install process-level signal handlers.
        yield received
        return

    def record(signum: int, frame: Any) -> None:
        if not received:
            received.append(signum)
            if not defer:
                interruption = KeyboardInterrupt()
                interruption.interruption_signal = signum
                raise interruption

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, record)
        yield received
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _stop(process: subprocess.Popen[bytes], signum: int) -> bool:
    """Bound cleanup; POSIX children run in a private session/process group."""

    def send(value: int) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, value)
            elif process.poll() is None:
                process.send_signal(value)
        except ProcessLookupError:
            pass

    send(signum)
    try:
        process.wait(timeout=CANCEL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # The direct child may have exited while descendants still hold the pipes.
    # Always address the group, including when the group leader is already gone.
    if os.name == "posix":
        send(signal.SIGKILL)
    elif process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def _drain(
    process: subprocess.Popen[bytes], out: BinaryIO, err: BinaryIO, cancelled: list[int]
) -> tuple[int | None, bool]:
    """Own the artifact writers; await pipe EOF or report an incomplete capture."""
    assert process.stdout is not None and process.stderr is not None
    ended_at = None
    with selectors.DefaultSelector() as selector:
        for pipe, target in ((process.stdout, out), (process.stderr, err)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, target)
        while selector.get_map() or process.poll() is None:
            if cancelled:
                return process.poll(), False
            code = process.poll()
            wait = 0.05
            if code is not None:
                if ended_at is None:
                    ended_at = time.monotonic()
                remaining = POST_EXIT_DRAIN_SECONDS - (time.monotonic() - ended_at)
                if remaining <= 0:
                    return code, False
                wait = min(wait, remaining)
            for key, _ in selector.select(wait):
                data = os.read(key.fd, 65536)
                if data:
                    key.data.write(data)
                else:
                    selector.unregister(key.fileobj)
        return process.poll(), not cancelled


def capture(command: list[str], directory: Path) -> dict[str, Any]:
    """Run argv once in the caller's cwd/environment; preserve raw streams."""
    with _cancellation() as cancelled, _operation("evidence.capture", directory) as run:
        if (
            not isinstance(command, list)
            or not command
            or not isinstance(command[0], str)
            or not command[0]
            or any(not isinstance(argument, str) or "\x00" in argument for argument in command)
        ):
            raise ValueError("command_requires_nonempty_text_argv_without_nul")
        for argument in command:
            # Preserve valid filesystem surrogate escapes, but reject values the OS
            # cannot encode before creating evidence artifacts.
            os.fsencode(argument)
        run.stage("spawn", required=True)
        return _capture(command, directory, cancelled, run)


def _capture(
    command: list[str], directory: Path, cancelled: list[int], run: _EvidenceRun
) -> dict[str, Any]:
    directory = _directory(directory)
    run.mutating = True
    stdout = directory / "stdout.log"
    stderr = directory / "stderr.log"
    error = None
    complete = False
    process = None
    reaped = None
    exit_code = None
    with stdout.open("xb") as out, stderr.open("xb") as err:
        os.chmod(stdout, 0o600)
        os.chmod(stderr, 0o600)
        try:
            if cancelled:
                raise KeyboardInterrupt
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            run.stage("drain")
            exit_code, complete = _drain(process, out, err, cancelled)
            if not complete:
                error = "output_incomplete"
        except (OSError, ValueError):
            exit_code = None
            error = "command_start_failed" if process is None else "capture_failed"
        except KeyboardInterrupt:
            exit_code = None
            error = "interrupted"
            if not cancelled:
                cancelled.append(signal.SIGINT)
        finally:
            if process is not None:
                code = process.poll()
                child_signalled = os.name == "posix" and code is not None and code < 0
                if cancelled or code is None or child_signalled:
                    forwarded = (
                        cancelled[0] if cancelled else -code if child_signalled else signal.SIGTERM
                    )
                    reaped = _stop(process, forwarded)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
        out.flush()
        err.flush()
        os.fsync(out.fileno())
        os.fsync(err.fileno())
    run.stage("hash")
    stdout_facts, stderr_facts = _facts(stdout), _facts(stderr)
    if cancelled:
        if process is not None and reaped is None:
            reaped = _stop(process, cancelled[0])
        error = "interrupted"
        complete = False
        exit_code = process.poll() if process is not None else None
    return run.receipt(
        directory,
        {
            "schema": "agent_evidence/v1",
            "kind": "command",
            "interruption_signal": cancelled[0] if cancelled else None,
            "termination_scope": "process_group" if os.name == "posix" else "direct_child",
            "direct_child_reaped": reaped,
            "status": "completed" if error is None else error,
            "directory": str(directory),
            "exit_code": exit_code,
            "output_complete": complete,
            "stdout": stdout_facts,
            "stderr": stderr_facts,
            "test_counts": None,
        },
    )


def write_cases(cases: list[dict[str, Any]], directory: Path) -> dict[str, Any]:
    """Retain every case as JSONL; 'pass' must be an actual boolean."""
    return _case_report(lambda: cases, directory)


def _case_report(load: Callable[[], Any], directory: Path) -> dict[str, Any]:
    with _cancellation(defer=False), _operation("evidence.cases", directory) as run:
        return _write_cases(load(), directory, run)


def _load_cases(source: str) -> Any:
    raw = sys.stdin.buffer.read() if source == "-" else Path(source).read_bytes()
    return json.loads(raw.decode("utf-8", errors="strict"))


def _write_cases(cases: list[dict[str, Any]], directory: Path, run: _EvidenceRun) -> dict[str, Any]:
    if not isinstance(cases, list) or not cases:
        raise ValueError("nonempty_case_array_required")
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("case"), str)
            or not case["case"].strip()
            or type(case.get("pass")) is not bool
        ):
            raise ValueError("case_requires_name_and_boolean_pass")
        encoded = json.dumps(case, ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8", errors="strict")
    run.stage("persist", required=True)
    directory = _directory(directory)
    run.mutating = True
    path = directory / "cases.jsonl"
    failures = directory / "failures.jsonl"
    failed = 0
    preview = []
    with path.open("xb") as out, failures.open("xb") as failure_out:
        os.chmod(path, 0o600)
        os.chmod(failures, 0o600)
        for index, case in enumerate(cases, 1):
            out.write((_json(case) + "\n").encode())
            if not case["pass"]:
                failed += 1
                failure_out.write((_json({"case_line": index, "result": case}) + "\n").encode())
                if len(preview) < 10:
                    name = case["case"]
                    preview.append(
                        {
                            "case_line": index,
                            "case": name[:120],
                            "name_shortened": len(name) > 120,
                        }
                    )
        for stream in (out, failure_out):
            stream.flush()
            os.fsync(stream.fileno())
    run.stage("hash")
    return run.receipt(
        directory,
        {
            "schema": "agent_evidence/v1",
            "kind": "cases",
            "status": "failed" if failed else "passed",
            "directory": str(directory),
            "total": len(cases),
            "passed": len(cases) - failed,
            "failed": failed,
            "cases": _facts(path),
            "failures": _facts(failures),
            "failure_preview": preview,
            "failure_preview_complete": failed == len(preview),
        },
    )


def summary(value: dict[str, Any], budget: int = DEFAULT_BUDGET) -> str:
    """Bound the entire serialized response, including its trailing newline."""
    if type(budget) is not int or not 512 <= budget <= 65536:
        raise ValueError("summary_budget_must_be_512_to_65536_bytes")
    result = dict(value)
    result["failure_preview"] = list(result.get("failure_preview", []))
    if "failure_preview" not in value:
        del result["failure_preview"]
    while len((_json(result) + "\n").encode()) > budget:
        preview = result.get("failure_preview")
        if not preview:
            raise ValueError("summary_metadata_exceeds_budget_use_receipt")
        preview.pop()
        result["failure_preview_complete"] = False
    return _json(result)


def read_page(path: Path, offset: int = 0, limit: int = 2000) -> dict[str, Any]:
    """Read a bounded byte range, retaining a progress cursor for long lines."""
    if type(offset) is not int or offset < 0:
        raise ValueError("offset_must_be_nonnegative_integer")
    if type(limit) is not int or not 4 <= limit <= MAX_READ_BYTES:
        raise ValueError("read_limit_must_be_4_to_12000_bytes")
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if offset > size:
            raise ValueError("offset_beyond_end")
        stream.seek(offset)
        raw = stream.read(limit)
    at_end = offset + len(raw) == size
    decoder = codecs.getincrementaldecoder("utf-8")("backslashreplace")
    text = decoder.decode(raw, final=at_end)
    pending, _ = decoder.getstate()
    next_offset = offset + len(raw) - len(pending)
    return {
        "file": str(path.absolute()),
        "offset": offset,
        "next_offset": next_offset,
        "size_bytes": size,
        "complete": next_offset == size,
        "encoding": "utf-8; invalid bytes escaped",
        "text": text,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("capture", help="Save raw command streams; preserve exit status")
    run.add_argument("--output-dir", required=True, type=Path)
    run.add_argument("--summary-bytes", type=int, default=DEFAULT_BUDGET)
    run.add_argument("command", nargs=argparse.REMAINDER)
    cases = commands.add_parser("cases", help="Save a JSON array of case/pass records as JSONL")
    cases.add_argument("--input", required=True, help="JSON file or - for standard input")
    cases.add_argument("--output-dir", required=True, type=Path)
    cases.add_argument("--summary-bytes", type=int, default=DEFAULT_BUDGET)
    read = commands.add_parser("read", help="Read a retained file by byte cursor without rerunning")
    read.add_argument("file", type=Path)
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit-bytes", type=int, default=2000, help="Byte page size: 4..12000")
    read.add_argument("--summary-bytes", type=int, default=DEFAULT_BUDGET)
    try:
        args = parser.parse_args(argv)
        # Validate before executing a command or writing artifacts.
        summary({}, args.summary_bytes)
        if args.action == "capture":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            value = capture(command, args.output_dir)
            if value["status"] == "interrupted":
                code = 128 + value["interruption_signal"]
            elif value["status"] != "completed" or value["exit_code"] is None:
                code = 2
            else:
                code = value["exit_code"]
                if code < 0:
                    code = 128 - code
        elif args.action == "cases":
            value = _case_report(lambda: _load_cases(args.input), args.output_dir)
            code = int(bool(value["failed"]))
        else:
            limit = args.limit_bytes
            while True:
                value = read_page(args.file, args.offset, limit)
                try:
                    summary(value, args.summary_bytes)
                    break
                except ValueError:
                    if limit <= 4:
                        raise
                    limit = max(4, limit // 2)
            code = 0
        print(summary(value, args.summary_bytes))
        return code
    except _HelpRequested:
        print(
            _json(
                {
                    "status": "help",
                    "commands": [
                        "capture --output-dir DIR [--summary-bytes N] -- COMMAND ...",
                        "cases --input JSON|- --output-dir DIR [--summary-bytes N]",
                        "read FILE [--offset N] [--limit-bytes 4..12000] [--summary-bytes N]",
                    ],
                    "summary_bytes": "512..65536; default 6000",
                }
            )
        )
        return 0
    except KeyboardInterrupt as exc:
        value = {"status": "interrupted"}
        if getattr(exc, "operation", None):
            value["operation"] = exc.operation
        print(_json(value), file=sys.stderr)
        return 128 + getattr(exc, "interruption_signal", signal.SIGINT)
    except Exception as exc:  # noqa: BLE001 - every CLI failure is bounded and non-reflecting
        # Avoid reflecting command arguments, raw records, or exception paths.
        value = {"status": "evidence_error", "error_type": type(exc).__name__}
        if getattr(exc, "operation", None):
            value["operation"] = exc.operation
        print(_json(value), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
