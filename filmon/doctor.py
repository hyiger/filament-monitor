from __future__ import annotations

import argparse
import collections
import math
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

def _serial_echo_check(port, baud, timeout_s: float = 5.0) -> bool:
    """Send an M118 A1 marker and wait for the printer to echo it back.

    Shared by --doctor (optional, non-fatal) and --self-test. Returns True when
    the echo is observed within timeout_s.
    """
    # write_timeout bounds the probe write; no flush() — tcdrain has no
    # timeout and a printer that stopped draining its CDC buffer would hang
    # the diagnostic before the GPIO checks ever ran.
    ser = serial.Serial(port, baud, timeout=0.5, write_timeout=2.0)
    try:
        token = f"filmon:selftest {int(time.time())}"
        ser.write(f"M118 A1 {token}\n".encode())
        print("  Sent:", token)
        print("  Waiting for echo...")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            line = ser.readline().decode(errors="replace").strip()
            if token.lower() in line.lower():
                return True
        return False
    finally:
        ser.close()


def run_doctor(args):
    """Run environment checks (GPIO availability, optional serial echo) and print diagnostics."""
    print("Doctor Mode (safe):")
    print("  - No M600 is sent.")
    print("  - Move filament to generate motion pulses.")
    print("  - Toggle runout to test runout.")
    print("  Ctrl+C to exit.")
    print()

    # Optional serial echo check (non-fatal). Runs only when -p/--port is given;
    # doctor mode remains usable without pyserial or a connected printer.
    if getattr(args, "port", None):
        print("Serial Echo Check (M118 A1):")
        if serial is None:
            print("  WARN: pyserial is not installed; skipping serial echo check.")
        else:
            try:
                if _serial_echo_check(args.port, args.baud):
                    print("  OK: echo seen")
                else:
                    print("  WARN: no echo observed")
            except Exception as e:
                print(f"  WARN: serial echo check failed: {e}")
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
        # pull_up=not active_high makes gpiozero's "active" mean asserted for
        # both polarities, so value == 1 always reads as "filament absent".
        runout = DigitalInputDevice(args.runout_gpio, pull_up=not args.runout_active_high)

    last_runout = None
    last_print = time.monotonic()


    # Optional: Rearm button test (short press = reset, long press = rearm)
    button_gpio = getattr(args, "rearm_button_gpio", None)
    if button_gpio is not None:
        active_high = bool(getattr(args, "rearm_button_active_high", False))
        long_s = float(getattr(args, "rearm_button_long_press", 1.5) or 1.5)
        debounce_s = float(getattr(args, "rearm_button_debounce", 0.25) or 0.25)

        def is_pressed(dev):
            # value is 1 when active; the pull_up choice below makes gpiozero's
            # "active" mean pressed for both wirings.
            return dev.value == 1

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

        btn = DigitalInputDevice(button_gpio, pull_up=not active_high)

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
                    # value is polarity-normalized by the pull_up choice above.
                    asserted = (runout.value == 1)
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
    """Safe hardware check: serial M118 echo round-trip plus pulse/runout observation.

    Does not construct the monitor and never sends pause G-code.
    """
    if not args.port:
        raise SystemExit("--self-test requires -p/--port")

    print("Self-Test")
    if _serial_echo_check(args.port, args.baud):
        print("  OK: echo seen")
    else:
        print("  WARN: no echo observed")

    # Motion pulse test
    motion = DigitalInputDevice(args.motion_gpio, pull_up=True)
    pulse_count = 0

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
        # pull_up=not active_high makes gpiozero's "active" mean asserted for
        # both polarities, so value == 1 always reads as "filament absent".
        runout = DigitalInputDevice(args.runout_gpio, pull_up=not args.runout_active_high)
        print("  Toggle runout (insert/remove) for 5 seconds...")
        last = None
        changes = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5.0:
            asserted = (runout.value == 1)
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
        "rearm_button_active_high": _get_cfg(cfg, "gpio", "rearm_button_active_high", False),
        "rearm_button_debounce": _get_cfg(cfg, "gpio", "rearm_button_debounce", 0.25),
        "rearm_button_long_press": _get_cfg(cfg, "gpio", "rearm_button_long_press", 1.5),
        "arm_min_pulses": _get_cfg(cfg, "detection", "arm_min_pulses", 12),
        "jam_timeout": _get_cfg(cfg, "detection", "jam_timeout", 8.0),
        "jam_timeout_adaptive": _get_cfg(cfg, "detection", "jam_timeout_adaptive", False),
        "jam_timeout_min": _get_cfg(cfg, "detection", "jam_timeout_min", 6.0),
        "jam_timeout_max": _get_cfg(cfg, "detection", "jam_timeout_max", 18.0),
        "jam_timeout_k": _get_cfg(cfg, "detection", "jam_timeout_k", 16.0),
        "jam_timeout_pps_floor": _get_cfg(cfg, "detection", "jam_timeout_pps_floor", 0.3),
        "jam_timeout_ema_halflife": _get_cfg(cfg, "detection", "jam_timeout_ema_halflife", 3.0),
        "arm_grace_pulses": _get_cfg(cfg, "detection", "arm_grace_pulses", 0),
        "arm_grace_s": _get_cfg(cfg, "detection", "arm_grace_s", 0.0),
        "pause_gcode": _get_cfg(cfg, "detection", "pause_gcode", "M600"),
        "verbose": _get_cfg(cfg, "logging", "verbose", False),
        "no_banner": _get_cfg(cfg, "logging", "no_banner", False),
        "json": _get_cfg(cfg, "logging", "json", False),
        "breadcrumb_interval": _get_cfg(cfg, "logging", "breadcrumb_interval", 2.0),
        "pulse_window": _get_cfg(cfg, "logging", "pulse_window", 2.0),
        "stall_thresholds": _get_cfg(cfg, "logging", "stall_thresholds", "3,6"),
        "control_socket": _get_cfg(cfg, "control", "socket", "/run/filmon/filmon.sock"),
    }


# Numeric argument fields coerced during validation. CLI values are already
# typed by argparse; these coercions catch untyped TOML values.
_INT_ARG_KEYS = (
    "baud",
    "motion_gpio",
    "runout_gpio",
    "rearm_button_gpio",
    "arm_min_pulses",
    "arm_grace_pulses",
)
_FLOAT_ARG_KEYS = (
    "runout_debounce",
    "rearm_button_debounce",
    "rearm_button_long_press",
    "jam_timeout",
    "jam_timeout_min",
    "jam_timeout_max",
    "jam_timeout_k",
    "jam_timeout_pps_floor",
    "jam_timeout_ema_halflife",
    "arm_grace_s",
    "breadcrumb_interval",
    "pulse_window",
)
# Boolean fields whose TOML values must be real booleans: a quoted
# "false" is a non-empty (truthy) string and would silently invert intent.
_BOOL_ARG_KEYS = (
    "runout_enabled",
    "runout_active_high",
    "rearm_button_active_high",
    "jam_timeout_adaptive",
    "verbose",
    "no_banner",
    "json",
)


def validate_args(args):
    """Coerce numeric values and fail fast on invalid configuration.

    TOML values reach argparse as untyped defaults, so numeric fields are
    coerced with int()/float() here. Non-numeric values and inconsistent
    combinations raise SystemExit with a clear message instead of surfacing
    as confusing behavior deep inside the monitor.
    """
    def _coerce(name, conv):
        v = getattr(args, name, None)
        if v is None:
            return None
        # Reject TOML booleans: bool is an int subclass but not a valid pin/timeout number.
        if isinstance(v, bool):
            raise SystemExit(f"Invalid value for {name}: {v!r} (expected a number)")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise SystemExit(f"Invalid value for {name}: {v!r} (expected a number)")
        # nan/inf are valid TOML floats but poison every timeout comparison
        # (elapsed >= nan is always False -> detection silently disabled).
        if not math.isfinite(f):
            raise SystemExit(f"Invalid value for {name}: {v!r} (must be a finite number)")
        # Integer fields must be integral: motion_gpio = 26.9 must not become pin 26.
        if conv is int and f != int(f):
            raise SystemExit(f"Invalid value for {name}: {v!r} (expected an integer)")
        v = conv(f)
        setattr(args, name, v)
        return v

    for name in _INT_ARG_KEYS:
        _coerce(name, int)
    for name in _FLOAT_ARG_KEYS:
        _coerce(name, float)

    for name in _BOOL_ARG_KEYS:
        v = getattr(args, name, None)
        if v is not None and not isinstance(v, bool):
            raise SystemExit(f"Invalid value for {name}: {v!r} (expected true or false)")

    if args.jam_timeout is not None and args.jam_timeout <= 0:
        raise SystemExit(f"jam_timeout must be > 0 (got {args.jam_timeout})")

    # Adaptive-timeout parameters must be positive: a zero/negative clamp or
    # coefficient makes the effective timeout <= 0, i.e. an instant false jam.
    if args.jam_timeout_min <= 0 or args.jam_timeout_max <= 0:
        raise SystemExit(
            f"jam_timeout_min/jam_timeout_max must be > 0 (got {args.jam_timeout_min}/{args.jam_timeout_max})"
        )
    if args.jam_timeout_k <= 0:
        raise SystemExit(f"jam_timeout_k must be > 0 (got {args.jam_timeout_k})")
    if args.jam_timeout_pps_floor <= 0:
        raise SystemExit(f"jam_timeout_pps_floor must be > 0 (got {args.jam_timeout_pps_floor})")
    if args.jam_timeout_ema_halflife < 0:
        raise SystemExit(f"jam_timeout_ema_halflife must be >= 0 (got {args.jam_timeout_ema_halflife})")
    if getattr(args, "jam_timeout_adaptive", False) and args.pulse_window <= 0:
        raise SystemExit("jam_timeout_adaptive requires pulse_window > 0 (pps is measured over that window)")

    if args.jam_timeout_min > args.jam_timeout_max:
        raise SystemExit(
            f"jam_timeout_min ({args.jam_timeout_min}) must be <= jam_timeout_max ({args.jam_timeout_max})"
        )

    # An omitted debounce means "no debounce": normalize to 0.0 so the
    # monitor's elapsed-time comparison never sees None. Only when runout is
    # enabled — while disabled, a non-None value would make the guardrails
    # warn about a --runout-debounce the user never supplied.
    if args.runout_enabled and args.runout_debounce is None:
        args.runout_debounce = 0.0

    for name in ("runout_debounce", "rearm_button_debounce", "arm_grace_s", "breadcrumb_interval"):
        v = getattr(args, name, None)
        if v is not None and v < 0:
            raise SystemExit(f"{name} must be >= 0 (got {v})")

    # A non-positive long-press threshold would classify EVERY release as a
    # long press, turning the documented short-press reset into a rearm.
    if args.rearm_button_long_press is not None and args.rearm_button_long_press <= 0:
        raise SystemExit(f"rearm_button_long_press must be > 0 (got {args.rearm_button_long_press})")

    pg = getattr(args, "pause_gcode", None)
    if pg is not None and (not isinstance(pg, str) or not pg.strip()):
        raise SystemExit(f"pause_gcode must be a non-empty G-code string (got {pg!r})")

    if args.arm_grace_pulses is not None and args.arm_grace_pulses < 0:
        raise SystemExit(f"arm_grace_pulses must be >= 0 (got {args.arm_grace_pulses})")

    # stall_thresholds must be a comma-separated list of numbers (e.g. "3,6").
    st = getattr(args, "stall_thresholds", None)
    if st:
        try:
            parsed = [float(x.strip()) for x in str(st).split(",") if x.strip()]
        except ValueError:
            raise SystemExit(
                f"Invalid stall_thresholds {st!r}: expected comma-separated seconds, e.g. \"3,6\""
            )
        # A nan entry wedges the stall breadcrumb index (comparisons with nan
        # never succeed), silencing all later thresholds.
        if any(not math.isfinite(x) for x in parsed):
            raise SystemExit(f"Invalid stall_thresholds {st!r}: entries must be finite numbers")

    return args


def resolved_config_dict(args) -> dict:
    return {
        "serial": {"port": args.port, "baud": args.baud},
        "gpio": {
            "motion_gpio": args.motion_gpio,
            "runout_enabled": args.runout_enabled,
            "runout_gpio": args.runout_gpio,
            "runout_active_high": args.runout_active_high,
            "runout_debounce": args.runout_debounce,
            "rearm_button_gpio": args.rearm_button_gpio,
            "rearm_button_debounce": args.rearm_button_debounce,
            "rearm_button_long_press": args.rearm_button_long_press,
            "rearm_button_active_high": getattr(args, "rearm_button_active_high", False),
        },
        "detection": {
            "arm_min_pulses": args.arm_min_pulses,
            "jam_timeout": args.jam_timeout,
            "pause_gcode": args.pause_gcode,
            "jam_timeout_adaptive": args.jam_timeout_adaptive,
            "jam_timeout_min": args.jam_timeout_min,
            "jam_timeout_max": args.jam_timeout_max,
            "jam_timeout_k": args.jam_timeout_k,
            "jam_timeout_pps_floor": args.jam_timeout_pps_floor,
            "jam_timeout_ema_halflife": args.jam_timeout_ema_halflife,
            "arm_grace_pulses": args.arm_grace_pulses,
            "arm_grace_s": args.arm_grace_s,
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


class _NoControlSocketAction(argparse.Action):
    """--no-control-socket: set the boolean dest AND clear control_socket.

    Setting both keeps direct build_arg_parser() consumers working (the flag
    immediately disables the socket) while the boolean dest lets parse_config()
    re-apply the disable after the TOML merge.
    """

    def __init__(self, option_strings, dest, **kwargs):
        kwargs.setdefault("default", False)
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, True)
        namespace.control_socket = ""


def build_arg_parser(defaults=None):
    """Construct the CLI argument parser for the daemon."""
    ap = argparse.ArgumentParser(epilog=USAGE_EXAMPLES, formatter_class=RawDescriptionHelpFormatter)
    # Defaults are the built-in defaults, optionally overlaid with TOML values by the
    # two-pass parse in cli.parse_config() — so explicit CLI flags always win.
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
    ap.add_argument("--runout-active-low", dest="runout_active_high", action="store_false",
                    help="Treat the runout signal as active-low (default; overrides a config file that sets active-high).")
    ap.add_argument("--doctor", action="store_true",
                    help="Run diagnostics and exit: GPIO pulse/runout/button checks, plus an M118 serial echo check when -p/--port is given (non-fatal).")
    ap.add_argument("--self-test", action="store_true",
                    help="Safe hardware check: serial M118 echo round-trip plus motion pulse/runout observation; the monitor is not started and no pause G-code is sent.")
    ap.add_argument("--pause-gcode", help="G-code to send when a jam/runout is detected.")
    ap.add_argument("--jam-timeout", type=float, help="Seconds without motion pulses (after arming) before declaring a jam.")
    ap.add_argument("--jam-timeout-adaptive", dest="jam_timeout_adaptive", action="store_true",
                    help="Scale the jam timeout with the recent pulse rate (k / max(pps_ema, pps_floor), clamped to [min, max]).")
    ap.add_argument("--no-jam-timeout-adaptive", dest="jam_timeout_adaptive", action="store_false",
                    help="Use the static --jam-timeout value.")
    ap.add_argument("--jam-timeout-min", type=float, help="Lower clamp (seconds) for the adaptive jam timeout.")
    ap.add_argument("--jam-timeout-max", type=float, help="Upper clamp (seconds) for the adaptive jam timeout.")
    ap.add_argument("--jam-timeout-k", type=float,
                    help="Adaptive timeout gain: effective timeout = k / max(pps_ema, pps_floor).")
    ap.add_argument("--jam-timeout-pps-floor", type=float,
                    help="Floor (pulses/sec) applied to the pps EMA in the adaptive timeout calculation.")
    ap.add_argument("--jam-timeout-ema-halflife", type=float,
                    help="Half-life (seconds) of the pulses-per-second EMA used by the adaptive timeout.")
    ap.add_argument("--arm-grace-pulses", type=int,
                    help="Suppress jam latching after arming until this many pulses are seen (0 disables).")
    ap.add_argument("--arm-grace-s", type=float,
                    help="Suppress jam latching for this many seconds after arming (0 disables). Releases together with --arm-grace-pulses on whichever comes first.")
    ap.add_argument("--arm-min-pulses", type=int, help="(Legacy/unused) Jam detection is marker-driven via filmon:arm.")
    ap.add_argument("--breadcrumb-interval", type=float,
                    help="Emit a low-volume heartbeat log every N seconds while enabled. Set 0 to disable.")
    ap.add_argument("--pulse-window", type=float,
                    help="Window (seconds) used to compute pulses-per-second (pps) for breadcrumbs.")
    ap.add_argument("--stall-thresholds", help="Comma-separated seconds-since-last-pulse thresholds for 'stall' breadcrumbs while armed.")
    sock_group = ap.add_mutually_exclusive_group()
    sock_group.add_argument("--control-socket", dest="control_socket",
                            help="Path to a local UNIX control socket (e.g. /run/filmon.sock). Use to rearm without sharing the printer serial port.")
    # Distinct boolean dest so an explicit CLI disable survives the TOML merge
    # (a const="" on control_socket would be clobbered by a configured socket path).
    # The action also clears control_socket directly so consumers that call
    # build_arg_parser()/parse_args() without parse_config() keep the old
    # "flag disables the socket" behavior.
    sock_group.add_argument("--no-control-socket", dest="no_control_socket", action=_NoControlSocketAction,
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


