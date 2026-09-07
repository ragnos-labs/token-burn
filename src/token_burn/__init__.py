"""Portable evidence, operation history, and guarded Git recovery.

Import submodules explicitly. Importing token_burn never starts an operation,
opens a journal, or initializes telemetry.
"""

__version__ = "0.1.0"
