#!/usr/bin/env python3
"""filament-monitor entrypoint (thin wrapper).

The implementation lives in the `filmon` package; this script remains for
backwards-compatible CLI usage and for test harnesses that load this file as a module.
"""

from __future__ import annotations

from filmon.cli import main
from filmon.doctor import build_arg_parser, load_toml_config, config_defaults_from, apply_runout_guardrails
import time
from filmon.monitor import FilamentMonitor
from filmon.state import MonitorState
from filmon.logging import JsonLogger
from filmon.constants import VERSION, USAGE_EXAMPLES, CONTROL_ENABLE, CONTROL_DISABLE, CONTROL_RESET, CONTROL_ARM, CONTROL_UNARM
from filmon.util import now_s
from filmon.gpio import DigitalInputDevice
import filmon.gpio as gpio
import filmon.monitor as monitor
import filmon.doctor as doctor
import filmon.serialio as serialio

__all__ = [
    "main",
    "build_arg_parser",
    "load_toml_config",
    "config_defaults_from",
    "apply_runout_guardrails",
    "FilamentMonitor",
    "MonitorState",
    "JsonLogger",
    "now_s",
    "time",
    "DigitalInputDevice",
    "gpio",
    "monitor",
    "doctor",
    "serialio",
]


if __name__ == "__main__":
    raise SystemExit(main())
