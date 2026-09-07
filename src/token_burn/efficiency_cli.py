# SPDX-License-Identifier: Apache-2.0
"""Small, path-free CLI projections of private efficiency artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from token_burn._local import ContractError, read_json, write_json


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ContractError("invalid_invocation")


def main(argv: list[str]) -> int:
    parser = _Parser(prog="token-burn")
    families = parser.add_subparsers(dest="family", required=True, parser_class=_Parser)
    usage_parser = families.add_parser(
        "usage", help="Collect explicit native usage or compare registered tasks"
    )
    usage_commands = usage_parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    collect_parser = usage_commands.add_parser("collect")
    for name in ("sources", "state-dir", "output"):
        collect_parser.add_argument("--" + name, type=Path, required=True)
    compare_parser = usage_commands.add_parser("compare")
    for name in ("baseline", "candidate", "outcomes", "output"):
        compare_parser.add_argument("--" + name, type=Path, required=True)
    handoff_parser = families.add_parser(
        "handoff", help="Create or validate a bounded local handoff"
    )
    handoff_commands = handoff_parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    create_parser = handoff_commands.add_parser("create")
    create_parser.add_argument("--input", type=Path, required=True)
    create_parser.add_argument("--output", type=Path, required=True)
    validate_parser = handoff_commands.add_parser("validate")
    validate_parser.add_argument("file", type=Path)
    profile_parser = families.add_parser(
        "profile", help="Read the released profile without installing it"
    )
    profile_commands = profile_parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    show_parser = profile_commands.add_parser("show")
    show_parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.family == "profile":
            from token_burn.profile import show

            record = show()
            print(
                json.dumps(record, sort_keys=True) if args.json else record["document"],
                end="\n" if args.json else "",
            )
            return 0
        if getattr(args, "output", None) is not None and args.output.exists():
            raise ContractError("output_already_exists")
        if args.family == "usage":
            from token_burn.usage import collect, compare

            if args.command == "collect":
                record = collect(read_json(args.sources), state_dir=args.state_dir)
                result = {
                    "schema_version": record["schema_version"],
                    "coverage": record["coverage"],
                    "manifest_sha256": record["manifest_sha256"],
                }
            else:
                record = compare(
                    read_json(args.baseline, max_bytes=16 * 1024 * 1024),
                    read_json(args.candidate, max_bytes=16 * 1024 * 1024),
                    read_json(args.outcomes),
                )
                result = {
                    "schema_version": record["schema_version"],
                    "status": record["status"],
                    "accepted": record["accepted"],
                    "matched_pairs": record["matched_pairs"],
                    "quality_status": record["quality_status"],
                    "coverage": record["coverage"],
                }
        else:
            from token_burn.handoff import create, validate

            if args.command == "validate":
                record = validate(read_json(args.file, max_bytes=32768))
                print(
                    json.dumps(
                        {
                            "schema_version": record["schema_version"],
                            "status": "valid",
                            "bytes": record["bytes"],
                        }
                    )
                )
                return 0
            supplied = read_json(args.input, max_bytes=32768)
            if not isinstance(supplied, dict) or not {"state"} <= set(supplied) <= {
                "state",
                "correlation",
            }:
                raise ContractError("invalid_handoff_input")
            record = create(supplied["state"], correlation=supplied.get("correlation"))
            result = {
                "schema_version": record["schema_version"],
                "status": "created",
                "bytes": record["bytes"],
                "preferred_target_exceeded": record["preferred_target_exceeded"],
            }
        result["output_sha256"] = write_json(args.output, record)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ContractError, OSError, TypeError, KeyError, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, ContractError) else "local_input_or_output_unavailable"
        print(json.dumps({"status": "error", "reason": reason}), file=sys.stderr)
        return 2
