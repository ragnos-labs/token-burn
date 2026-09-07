# SPDX-License-Identifier: Apache-2.0
"""One installed entrypoint for evidence, history, and guarded worktree recovery."""

from __future__ import annotations

import os
import sys

from token_burn import __version__

HELP = """token-burn: keep the evidence, return the useful part.

Evidence:
  capture --output-dir DIR -- COMMAND ...   Run once; save stdout, stderr and receipt
  cases --input JSON|- --output-dir DIR     Retain all case results; summarize counts
  read FILE --offset N --limit-bytes N      Read a bounded page without rerunning

Operation history:
  status RUN_ID                             Inspect an operation's outcome and delivery
  history RUN_ID --offset N --limit N        Read a bounded page of recorded events
  replay RUN_ID                             Retry telemetry, never the original action
  snapshot                                  Inspect a bounded local inventory
  metrics                                   Print Prometheus metrics without starting a server

Git recovery:
  worktree --help                           Archive, restore and guard worktree removal

Network export is off by default. Run a command with --help for its options.
Documentation: https://github.com/ragnos-labs/token-burn
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(f"token-burn {__version__}")
        return 0
    if not args or args in (["-h"], ["--help"]):
        print(HELP)
        return 0
    if os.name != "posix":
        print("token-burn requires a POSIX system (macOS or Linux).", file=sys.stderr)
        return 2
    if args[0] in {"capture", "cases", "read"}:
        from token_burn.evidence import _main

        return _main(args)
    if args[0] in {"status", "history", "replay", "snapshot", "metrics"}:
        from token_burn.history_cli import main as history_main

        if args[0] == "history":
            args[0] = "read"
        return history_main(args)
    if args[0] == "worktree":
        from token_burn.worktree.cli import main as worktree_main

        return worktree_main(args[1:])
    print('{"status":"error","error_type":"InvalidCommand"}', file=sys.stderr)
    return 2
