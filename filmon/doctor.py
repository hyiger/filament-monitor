from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import signal
import socket
import sys
import threading
import time
from argparse import RawDescriptionHelpFormatter
from typing import Optional

try:
    import tomllib  # py3.11+
except Exception:  # pragma: no cover
    tomllib = None
    try:
        import tomli as _tomli  # type: ignore
    except Exception:
        _tomli = None



from .gpio import DigitalInputDevice
from .serialio import serial
from .util import now_s
from .constants import VERSION, CONTROL_ENABLE, CONTROL_DISABLE, CONTROL_RESET, CONTROL_ARM, CONTROL_UNARM, USAGE_EXAMPLES

def run_doctor(args):
    """Run environment checks (serial access, GPIO availability) and print diagnostics."""
    print("Doctor Mode (safe):")
    print("  - No M600 is sent.")
    print("  - Move filament to generate motion pulses.")
    print("  - Toggle runout to test runout.")
    print("  Ctrl+C to exit.")
    print()

    motion = DigitalInputDevice(args.motion_gpio, pull_up=True)
    pulse_count = 0

    def on_pulse():
        """Increment the local pulse counter for this diagnostic test."""
        nonlocal pulse_count
        pulse_count += 1

    motion.when_deactivated = on_pulse

    runout = None
    if args.runout_enabled:
        runout = DigitalInputDevice(args.runout_gpio, pull_up=True)

    last_runout = None
    last_print = time.monotonic()


    # Optional: Rearm button test (short press = reset, long press = rearm)
    button_gpio = getattr(args, "rearm_button_gpio", None)
    if button_gpio is not None:
        active_high = bool(getattr(args, "rearm_button_active_high", False))
        long_s = float(getattr(args, "rearm_button_long_press", 1.5) or 1.5)
        debounce_s = float(getattr(args, "rearm_button_debounce", 0.25) or 0.25)

        def is_pressed(dev):
            v = dev.value
            return (v == 1) if active_high else (v == 0)

        def wait_for_state(dev, pressed: bool, timeout_s: float):
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if is_pressed(dev) == pressed:
                    return True
                time.sleep(0.01)
            return False

        print()
        print("Rearm Button Test (optional)")
        print(f"  GPIO={button_gpio} active_high={active_high} long_press_s={long_s:.2f} debounce_s={debounce_s:.2f}")
        print("  This test is read-only: it does not change monitor state or send any G-code.")
        print()

        btn = DigitalInputDevice(button_gpio, pull_up=True)

        # Ensure button starts released
        if is_pressed(btn):
            print("  WARN: button appears pressed at start. Please release it...")
            if not wait_for_state(btn, pressed=False, timeout_s=10.0):
                print("  WARN: button still appears pressed; skipping button test.")
            else:
                time.sleep(debounce_s)

        # Idle stability check
        unstable = False
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            if is_pressed(btn):
                unstable = True
                break
            time.sleep(0.01)

        if unstable:
            print("  WARN: button input toggled/pressed during idle check. Wiring/pull-up may be incorrect.")
        else:
            print("  OK: idle state stable (not pressed)")

        # Short press test
        print("  ACTION: short press (tap) the button now...")
        if not wait_for_state(btn, pressed=True, timeout_s=10.0):
            print("  WARN: no button press detected (short press test skipped)")
        else:
            t_press = time.monotonic()
            if not wait_for_state(btn, pressed=False, timeout_s=10.0):
                print("  WARN: button press detected but no release observed (short press test failed)")
            else:
                t_release = time.monotonic()
                dur = t_release - t_press
                time.sleep(debounce_s)
                if dur >= long_s:
                    print(f"  WARN: detected a long press ({dur:.2f}s) during short-press test; try a quicker tap.")
                else:
                    print(f"  OK: short press detected ({dur:.2f}s) => would trigger reset")

        # Long press test
        print("  ACTION: long press (hold) the button now, then release...")
        if not wait_for_state(btn, pressed=True, timeout_s=10.0):
            print("  WARN: no button press detected (long press test skipped)")
        else:
            t_press = time.monotonic()
            # Wait until long-press threshold is reached (still pressed)
            reached = False
            deadline = t_press + long_s + 10.0
            while time.monotonic() < deadline:
                if not is_pressed(btn):
                    break
                if time.monotonic() - t_press >= long_s:
                    reached = True
                    break
                time.sleep(0.01)

            if not reached:
                dur = time.monotonic() - t_press
                print(f"  WARN: press released before long-press threshold ({dur:.2f}s < {long_s:.2f}s)")
            else:
                # Require release to complete the gesture
                if not wait_for_state(btn, pressed=False, timeout_s=10.0):
                    print("  WARN: long press threshold reached but no release observed (long press test failed)")
                else:
                    t_release = time.monotonic()
                    dur = t_release - t_press
                    time.sleep(debounce_s)
                    print(f"  OK: long press detected ({dur:.2f}s) => would trigger rearm")

        print()
    try:
        while True:
            if time.monotonic() - last_print >= 0.5:
                if runout is not None:
                    asserted = (runout.value == 1) if args.runout_active_high else (runout.value == 0)
                    if asserted != last_runout:
                        print(f"  RUNOUT asserted={asserted}")
                        last_runout = asserted
                    print(f"  motion_pulses={pulse_count} runout_asserted={asserted}")
                else:
                    print(f"  motion_pulses={pulse_count} runout_asserted=N/A")
                last_print = time.monotonic()
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass


def run_self_test(args):
    """Exercise the monitor control-marker path and basic state transitions."""
    if not args.port:
        raise SystemExit("--self-test requires -p/--port")

    ser = serial.Serial(args.port, args.baud, timeout=0.5)
    token = f"filmon:selftest {int(time.time())}"
    ser.write(f"M118 A1 {token}\n".encode())
    ser.flush()

    print("Self-Test")
    print("  Sent:", token)
    print("  Waiting for echo...")

    deadline = time.monotonic() + 5.0
    echoed = False
    while time.monotonic() < deadline:
        line = ser.readline().decode(errors="replace").strip()
        if token.lower() in line.lower():
            echoed = True
            print("  OK: echo seen")
            break

    if not echoed:
        print("  WARN: no echo observed")

    # Motion pulse test
    motion = DigitalInputDevice(args.motion_gpio, pull_up=True)
    pulse_count = 0

    """Increment the local pulse counter for this diagnostic test."""
    def on_pulse():
        """Increment a local pulse counter for this diagnostic test."""
        nonlocal pulse_count
        pulse_count += 1

    motion.when_deactivated = on_pulse

    print("  Roll filament for 3 seconds...")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 3.0:
        time.sleep(0.01)
    print("  Motion pulses:", pulse_count)

    # Runout transition test (safe)
    if not args.runout_enabled:
        print("  Runout test: skipped (runout disabled)")
    else:
        runout = DigitalInputDevice(args.runout_gpio, pull_up=True)
        print("  Toggle runout (insert/remove) for 5 seconds...")
        last = None
        changes = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5.0:
            asserted = (runout.value == 1) if args.runout_active_high else (runout.value == 0)
            if last is None:
                last = asserted
            elif asserted != last:
                print(f"  RUNOUT asserted={asserted}")
                last = asserted
                changes += 1
            time.sleep(0.02)

        if changes == 0:
            print("  WARN: no runout transitions observed (check wiring/polarity).")
        else:
            print(f"  OK: runout transitions observed ({changes}).")

    ser.close()
    print("Self-test complete.")


def load_toml_config(path: str) -> dict:
    """Load TOML configuration from path."""
    with open(path, "rb") as f:
        if tomllib is not None:
            return tomllib.load(f)
        if _tomli is not None:  # pragma: no cover
            return _tomli.load(f)
        raise RuntimeError("TOML support not available; install 'tomli' or use Python 3.11+")


def _get_cfg(cfg: dict, section: str, key: str, default=None):
    sec = cfg.get(section, {})
    if not isinstance(sec, dict):
        return default
    return sec.get(key, default)


def config_defaults_from(cfg: dict) -> dict:
    """Map TOML config into argparse defaults."""
    return {
        "port": _get_cfg(cfg, "serial", "port", None),
        "baud": _get_cfg(cfg, "serial", "baud", 115200),
        "motion_gpio": _get_cfg(cfg, "gpio", "motion_gpio", 26),
        "runout_enabled": _get_cfg(cfg, "gpio", "runout_enabled", False),
        "runout_gpio": _get_cfg(cfg, "gpio", "runout_gpio", 27),
        "runout_active_high": _get_cfg(cfg, "gpio", "runout_active_high", False),
        "runout_debounce": _get_cfg(cfg, "gpio", "runout_debounce", None),
        "rearm_button_gpio": _get_cfg(cfg, "gpio", "rearm_button_gpio", None),
        "rearm_button_active_high": _get_cfg(cfg, "gpio", "rearm_button_active_high", True),
        "rearm_button_debounce": _get_cfg(cfg, "gpio", "rearm_button_debounce", 0.25),
        "arm_min_pulses": _get_cfg(cfg, "detection", "arm_min_pulses", 12),
        "jam_timeout": _get_cfg(cfg, "detection", "jam_timeout", 8.0),
        "pause_gcode": _get_cfg(cfg, "detection", "pause_gcode", "M600"),
        "verbose": _get_cfg(cfg, "logging", "verbose", False),
        "no_banner": _get_cfg(cfg, "logging", "no_banner", False),
        "json": _get_cfg(cfg, "logging", "json", False),
        "breadcrumb_interval": _get_cfg(cfg, "logging", "breadcrumb_interval", 2.0),
        "pulse_window": _get_cfg(cfg, "logging", "pulse_window", 2.0),
        "stall_thresholds": _get_cfg(cfg, "logging", "stall_thresholds", "3,6"),
        "control_socket": _get_cfg(cfg, "control", "socket", "/run/filmon/filmon.sock"),
    }


def resolved_config_dict(args) -> dict:
    return {
        "serial": {"port": args.port, "baud": args.baud},
        "gpio": {
            "motion_gpio": args.motion_gpio,
            "runout_enabled": args.runout_enabled,
            "runout_gpio": args.runout_gpio,
            "runout_active_high": args.runout_active_high,
            "runout_debounce": args.runout_debounce,
        },
        "detection": {
            "arm_min_pulses": args.arm_min_pulses,
            "jam_timeout": args.jam_timeout,
            "pause_gcode": args.pause_gcode,
        },
        "logging": {
            "verbose": args.verbose,
            "no_banner": args.no_banner,
            "breadcrumb_interval": args.breadcrumb_interval,
            "pulse_window": args.pulse_window,
            "stall_thresholds": args.stall_thresholds,
            "json": bool(args.json),
        },
        "control": {
            "socket": getattr(args, "control_socket", None),
        },
    }


def build_arg_parser(defaults=None):
    """Construct the CLI argument parser for the daemon."""
    ap = argparse.ArgumentParser(epilog=USAGE_EXAMPLES, formatter_class=RawDescriptionHelpFormatter)
    # Defaults are sourced from the built-in defaults, and optionally overridden by TOML config
    # (we backfill unset CLI args after parsing).
    if defaults is None:
        defaults = config_defaults_from({})
    ap.set_defaults(**defaults)
    ap.add_argument("-p", "--port", help="Serial device for the printer connection (e.g., /dev/ttyACM0).")
    ap.add_argument("--baud", type=int, help="Serial baud rate for the printer connection.")
    ap.add_argument("--motion-gpio", type=int, help="BCM GPIO pin number for the filament motion pulse input.")
    ap.add_argument("--runout-gpio", type=int, help="BCM GPIO pin number for the optional runout input.")
    ap.add_argument("--runout-enabled", dest="runout_enabled", action="store_true", help="Enable runout monitoring (default: disabled).")
    ap.add_argument("--runout-disabled", dest="runout_enabled", action="store_false", help="Disable runout monitoring.")
    ap.add_argument("--runout-debounce", type=float, help="Debounce time (seconds) applied to the runout input to ignore short glitches.")

    ap.add_argument("--rearm-button-gpio", type=int,
                help="Optional BCM GPIO pin for a physical rearm button (e.g., 25).")
    ap.add_argument("--rearm-button-debounce", type=float,
                help="Debounce time for rearm button presses in seconds (default: 0.25).")

    ap.add_argument("--rearm-button-long-press", type=float,
                help="Long-press threshold in seconds (default: 1.5). Short press resets; long press rearms.")

    ap.add_argument("--verbose", dest="verbose", action="store_true", help="Verbose logging (includes serial chatter).")
    ap.add_argument("--no-verbose", dest="verbose", action="store_false", help="Disable verbose logging.")
    json_group = ap.add_mutually_exclusive_group()
    json_group.add_argument("--json", dest="json", action="store_true", help="Emit JSON log events.")
    json_group.add_argument("--no-json", dest="json", action="store_false", help="Disable JSON log output.")
    ap.add_argument("--no-banner", dest="no_banner", action="store_true", help="Disable the startup banner.")
    ap.add_argument("--banner", dest="no_banner", action="store_false", help="Enable the startup banner.")
    ap.add_argument("--runout-active-high", action="store_true", help="Treat the runout signal as active-high.")
    ap.add_argument("--doctor", action="store_true", help="Run host/printer diagnostics (GPIO + serial checks) and exit.")
    ap.add_argument("--self-test", action="store_true", help="Dry-run mode: monitor inputs and parsing but do not send pause commands.")
    ap.add_argument("--pause-gcode", help="G-code to send when a jam/runout is detected.")
    ap.add_argument("--jam-timeout", type=float, help="Seconds without motion pulses (after arming) before declaring a jam.")
    ap.add_argument("--arm-min-pulses", type=int, help="(Legacy/unused) Jam detection is marker-driven via filmon:arm.")
    ap.add_argument("--breadcrumb-interval", type=float,
                    help="Emit a low-volume heartbeat log every N seconds while enabled. Set 0 to disable.")
    ap.add_argument("--pulse-window", type=float,
                    help="Window (seconds) used to compute pulses-per-second (pps) for breadcrumbs.")
    ap.add_argument("--stall-thresholds", help="Comma-separated seconds-since-last-pulse thresholds for 'stall' breadcrumbs while armed.")
    sock_group = ap.add_mutually_exclusive_group()
    sock_group.add_argument("--control-socket", dest="control_socket",
                            help="Path to a local UNIX control socket (e.g. /run/filmon.sock). Use to rearm without sharing the printer serial port.")
    sock_group.add_argument("--no-control-socket", dest="control_socket", action="store_const", const="",
                            help="Disable the local control socket.")
    ap.add_argument("--config", help="Path to a TOML config file. CLI args override config values.")
    ap.add_argument("--print-config", action="store_true", help="Print the resolved configuration and exit.")
    ap.add_argument("--version", action="store_true", help="Print version and exit.")
    return ap


def apply_runout_guardrails(args):
    """Apply CLI guardrails for runout-related flags.

    Runout monitoring is disabled by default. This function makes runout-related
    settings no-ops unless --runout-enabled is set, and returns the list of ignored
    flags for optional consolidated warning output.

    Args:
        args: Parsed argparse namespace.

    Returns:
        A sorted list of ignored runout-related flag names (strings).
    """
    ignored = []

    if getattr(args, "runout_gpio", None) is not None and not getattr(args, "runout_enabled", False):
        ignored.append("--runout-gpio")
        args.runout_gpio = None

    if getattr(args, "runout_debounce", None) is not None and not getattr(args, "runout_enabled", False):
        ignored.append("--runout-debounce")
        args.runout_debounce = None

    if getattr(args, "runout_active_high", False) and not getattr(args, "runout_enabled", False):
        ignored.append("--runout-active-high")
        args.runout_active_high = False

    return sorted(set(ignored))


