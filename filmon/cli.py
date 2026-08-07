from __future__ import annotations

import argparse
import sys
import json


try:
    import serial  # pyserial
except Exception:  # pragma: no cover
    serial = None

from .constants import VERSION, USAGE_EXAMPLES

from .doctor import (
    apply_runout_guardrails,
    build_arg_parser,
    config_defaults_from,
    load_toml_config,
    run_doctor,
    run_self_test,
    resolved_config_dict,
    validate_args,
)
from .gpio import init_gpio
from .logging import JsonLogger
from .monitor import FilamentMonitor
from .state import MonitorState
from .util import now_s
import threading
import signal
import time

def parse_config(argv=None):
    """Parse and merge configuration with CLI > TOML > built-in precedence.

    Two-pass parse: a minimal pre-parser extracts only --config, the TOML file
    (if any) is loaded, and the real parser is built with those values as
    argparse defaults — so explicitly passed CLI flags naturally win.

    Returns the validated argparse namespace. Raises SystemExit with a clear
    message on invalid values (see doctor.validate_args).
    """
    if argv is None:
        argv = sys.argv[1:]

    # Pass 1: locate --config without tripping over the other flags.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    pre_args, _ = pre.parse_known_args(argv)

    try:
        cfg = load_toml_config(pre_args.config) if pre_args.config else {}
    except (OSError, ValueError) as e:
        raise SystemExit(f"Cannot load config file {pre_args.config!r}: {e}")

    # Pass 2: real parse with TOML values as defaults.
    ap = build_arg_parser(defaults=config_defaults_from(cfg))
    args = ap.parse_args(argv)

    # --no-control-socket has its own boolean dest so the explicit CLI disable
    # survives the merge: apply it after parsing.
    if getattr(args, "no_control_socket", False):
        args.control_socket = ""

    validate_args(args)
    return args


def main():
    """CLI entry point. Parses args, configures the monitor, and starts the daemon."""
    if len(sys.argv) == 1:
        build_arg_parser().print_help()
        return 0

    args = parse_config(sys.argv[1:])

    # Informational modes run before any dependency checks or warnings.
    # Print resolved configuration and exit (does not require pyserial/GPIO).
    if getattr(args, "print_config", False):
        print(json.dumps(resolved_config_dict(args), indent=2, sort_keys=True))
        return 0

    if args.version:
        print(VERSION)
        return 0

    # Runout option guardrails
    # - Runout monitoring is disabled by default.
    # - Runout GPIO, debounce, and polarity are no-ops unless runout is enabled.
    ignored_runout_flags = apply_runout_guardrails(args)
    # The built-in --runout-gpio default (27) is harmless while runout is
    # disabled; only warn about it when the user explicitly passed the flag.
    if "--runout-gpio" not in sys.argv:
        ignored_runout_flags = [f for f in ignored_runout_flags if f != "--runout-gpio"]
    if ignored_runout_flags:
        print("WARNING: runout monitoring is disabled; ignoring: " + ", ".join(ignored_runout_flags))

    # Real GPIO is required beyond this point (--doctor, --self-test, and
    # normal monitoring all read pins). --print-config and --version above
    # stay GPIO-free. Refuse to run on the no-op stub: a daemon that monitors
    # nothing would false-jam every print.
    if init_gpio() == "stub":
        print(
            "ERROR: no usable GPIO backend (gpiozero is not installed). "
            "Install gpiozero and lgpio to run on hardware.",
            file=sys.stderr,
        )
        return 2

    # Doctor mode is GPIO-first and must work without pyserial installed
    # (its serial echo check is optional and non-fatal).
    if args.doctor:
        run_doctor(args)
        return 0

    # Serial (pyserial) is required to connect to the printer.
    if serial is None:  # pragma: no cover
        print("ERROR: pyserial is not installed. Install it with: pip install pyserial", file=sys.stderr)
        return 2

    if args.self_test:
        run_self_test(args)
        return 0

    if not args.port:
        raise SystemExit("Normal mode requires -p/--port")

    # write_timeout bounds _send_gcode when the printer stops draining its CDC
    # buffer (kill screen, mid-flash): the write raises SerialTimeoutException
    # instead of blocking forever while holding the serial lock.
    ser = serial.Serial(args.port, args.baud, timeout=0.25, write_timeout=2.0)
    state = MonitorState(serial_connected=True, serial_port=args.port, baud=args.baud)
    logger = JsonLogger(enable_json=bool(getattr(args, "json", False)))
    mon = FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=args.motion_gpio,
        runout_gpio=(args.runout_gpio if args.runout_enabled else None),
        runout_active_high=args.runout_active_high,
        runout_debounce_s=args.runout_debounce,
        jam_timeout_s=args.jam_timeout,
        jam_timeout_adaptive=args.jam_timeout_adaptive,
        jam_timeout_min_s=args.jam_timeout_min,
        jam_timeout_max_s=args.jam_timeout_max,
        jam_timeout_k=args.jam_timeout_k,
        jam_timeout_pps_floor=args.jam_timeout_pps_floor,
        jam_timeout_ema_halflife_s=args.jam_timeout_ema_halflife,
        arm_grace_pulses=args.arm_grace_pulses,
        arm_grace_s=args.arm_grace_s,
        arm_min_pulses=args.arm_min_pulses,
        pause_gcode=args.pause_gcode,
        verbose=args.verbose,
        breadcrumb_interval_s=args.breadcrumb_interval,
        pulse_window_s=args.pulse_window,
        stall_thresholds_s=args.stall_thresholds,
        rearm_button_gpio=args.rearm_button_gpio,
        rearm_button_active_high=getattr(args, "rearm_button_active_high", False),
        rearm_button_debounce_s=args.rearm_button_debounce,
        rearm_button_long_press_s=args.rearm_button_long_press,
    )

    if not args.no_banner:
        print(f"filament-monitor {VERSION}")
        print("For Generic Marlin-compatible printer")
        # Structured startup event for log scraping
        logger.emit(
            "startup",
            version=VERSION,
            port=args.port,
            baud=args.baud,
            motion_gpio=args.motion_gpio,
            runout_gpio=(args.runout_gpio if args.runout_enabled else None),
            runout_active_high=args.runout_active_high,
            arm_min_pulses=args.arm_min_pulses,
            jam_timeout_s=args.jam_timeout,
            jam_timeout_adaptive=args.jam_timeout_adaptive,
            jam_timeout_min_s=args.jam_timeout_min,
            jam_timeout_max_s=args.jam_timeout_max,
            jam_timeout_k=args.jam_timeout_k,
            jam_timeout_pps_floor=args.jam_timeout_pps_floor,
            jam_timeout_ema_halflife_s=args.jam_timeout_ema_halflife,
            arm_grace_pulses=args.arm_grace_pulses,
            arm_grace_s=args.arm_grace_s,
            pause_gcode=args.pause_gcode,
            verbose=args.verbose,
            control_socket=getattr(args, "control_socket", None),
        )

    mon.attach_serial(ser)
    mon.start_serial_reader(verbose=args.verbose, port=args.port, baud=args.baud)
    # Local control socket (for re-arming/resetting without a second serial connection)
    if getattr(args, "control_socket", None):
        mon.start_control_socket(args.control_socket)
    mon.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    exit_code = 0
    while not stop.is_set():
        t = getattr(mon, "_serial_thread", None)
        # Check the monitor loop FIRST: a loop failure sets the shared stop
        # event, which also ends the serial reader — checking the reader first
        # would misreport that cascade as exit 3 and hide the real fault.
        lt = getattr(mon, "_loop_thread", None)
        if lt is not None and not lt.is_alive():
            # The main loop is the jam detector; without it the daemon only
            # looks healthy. Exit non-zero so systemd restarts us.
            logger.emit("monitor_loop_dead")
            exit_code = 4
            stop.set()
            break
        if t is not None and not t.is_alive():
            # The reader reconnects on its own (a reconnecting thread is still
            # alive), so a dead thread means it truly crashed unexpectedly.
            logger.emit("serial_thread_dead")
            exit_code = 3
            stop.set()
            break
        time.sleep(0.2)

    mon.stop()
    # Join worker threads before closing the port: closing mid-write is
    # unsupported by pyserial and can truncate a final pause command.
    for name in ("_serial_thread", "_loop_thread", "_control_thread"):
        t = getattr(mon, name, None)
        if t is not None:
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
    # Close whichever port is currently attached (a reconnect may have swapped
    # in a fresh one); hold the write lock so we never close mid-write.
    with mon._ser_lock:
        try:
            (mon._ser or ser).close()
        except Exception:
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
