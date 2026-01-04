#!/usr/bin/env python3
"""filament-monitor entrypoint (thin wrapper).

The implementation lives in the `filmon` package; this script remains for
backwards-compatible CLI usage and for test harnesses that load this file as a module.

This file also re-exports a small set of symbols used by the unit/integration
tests in this repository (and any third-party wrappers that relied on the
previous monolithic layout).
"""

import time

from filmon.cli import build_arg_parser, main
from filmon import monitor as monitor
from filmon.state import MonitorState
from filmon.monitor import FilamentMonitor
from filmon.logging import JsonLogger
from filmon.constants import CONTROL_RESET
from filmon.doctor import apply_runout_guardrails


__all__ = [
    "time",
    "build_arg_parser",
    "main",
    "monitor",
    "MonitorState",
    "FilamentMonitor",
    "JsonLogger",
    "CONTROL_RESET",
    "apply_runout_guardrails",
]


if __name__ == "__main__":
    raise SystemExit(main())
