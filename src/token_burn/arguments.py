"""Bounded argument errors that never echo arbitrary caller input."""

import argparse
import sys


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print('{"status":"error","error_type":"InvalidInvocation"}', file=sys.stderr)
        raise SystemExit(2)
