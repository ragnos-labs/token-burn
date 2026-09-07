# SPDX-License-Identifier: Apache-2.0
#!/usr/bin/env python3
"""Read or explicitly replay private agent operation evidence by run ID.

No command starts work, grants cleanup authority, or marks backend verification.
Replay retains permanent/partial collector rejections as held evidence. An
interrupted replay can resend identical span IDs and log event IDs.
"""

from __future__ import annotations

import json

from token_burn.arguments import ArgumentParser
from token_burn.operations import Operation  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="token-burn", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "read", "replay"):
        command = subparsers.add_parser(name)
        command.add_argument("run_id")
        if name == "read":
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=50)
        if name == "replay":
            command.add_argument(
                "--limit", type=int, default=20, help="Maximum HTTP requests (1-100)."
            )
            command.add_argument("--timeout", type=float, default=0.5)
            command.add_argument("--max-seconds", type=float, default=3.0)
    for name in ("snapshot", "metrics"):
        command = subparsers.add_parser(name, help="Read-only bounded private journal inventory.")
        command.add_argument("--limit", type=int, default=100)
        command.add_argument("--max-events", type=int, default=200)
        command.add_argument("--max-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        if args.command in {"snapshot", "metrics"}:
            from token_burn.monitor import metrics_text, snapshot

            value = snapshot(
                limit=args.limit, max_events=args.max_events, max_seconds=args.max_seconds
            )
            if args.command == "metrics":
                print(metrics_text(value), end="")
            else:
                print(json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True))
            return 0 if value["complete"] else 1
        operation = Operation.open(args.run_id)
        if args.command == "status":
            value = operation.status()
        elif args.command == "read":
            value = operation.read(offset=args.offset, limit=args.limit)
        else:
            value = operation.export_pending(
                limit=args.limit, timeout=args.timeout, max_seconds=args.max_seconds
            )
        print(json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True))
        return 1 if value.get("status") == "error" else 0
    except Exception as exc:  # noqa: BLE001 - no raw paths, input, or exception text
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
