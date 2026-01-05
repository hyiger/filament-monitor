from __future__ import annotations

import sys
import json


try:
    import serial  # pyserial
except Exception:  # pragma: no cover
    serial = None

from .constants import VERSION, USAGE_EXAMPLES

from .doctor import (
    build_arg_parser,
    config_defaults_from,
    load_toml_config,
    run_doctor,
    run_self_test,
    resolved_config_dict,
)
from .logging import JsonLogger
from .monitor import FilamentMonitor
from .state import MonitorState
from .util import now_s
import threading
import signal
import time

def main():
    detection_cfg = {}
    """CLI entry point. Parses args, configures the monitor, and starts the daemon."""
    ap = build_arg_parser()
    if len(sys.argv) == 1:
        ap.print_help()
        return 0

    args = ap.parse_args()

    # Apply TOML configuration (if provided). CLI arguments take precedence.
    # Note: We only fill values that are unset/empty on the CLI.
    if getattr(args, "config", None):
        cfg = load_toml_config(args.config)
        defaults = config_defaults_from(cfg)
        for k, v in defaults.items():
            # Only backfill fields that are unset/empty from the CLI.
            if getattr(args, k, None) in (None, ""):
                setattr(args, k, v)

    # Print resolved configuration and exit (does not require pyserial/GPIO).
    if getattr(args, "print_config", False):
        print(json.dumps(resolved_config_dict(args), indent=2, sort_keys=True))
        return 0

    # Serial (pyserial) is required to connect to the printer.
    if serial is None:  # pragma: no cover
        print("ERROR: pyserial is not installed. Install it with: pip install pyserial", file=sys.stderr)
        return 2


    # Runout option guardrails
    # - Runout monitoring is disabled by default.
    # - --runout-gpio defaults to 27 and is only used when runout is enabled.
    # - Runout debounce and polarity only apply when runout is enabled.

    ignored_runout_flags = []
    if getattr(args, "runout_gpio", None) is not None and not getattr(args, "runout_enabled", False):
        # Only warn if the user explicitly provided --runout-gpio. The default is harmless.
        if "--runout-gpio" in sys.argv:
            ignored_runout_flags.append("--runout-gpio")
        args.runout_gpio = None

    if getattr(args, "runout_debounce", None) is not None and not getattr(args, "runout_enabled", False):
        ignored_runout_flags.append("--runout-debounce")
        args.runout_debounce = None

    if getattr(args, "runout_active_high", False) and not getattr(args, "runout_enabled", False):
        ignored_runout_flags.append("--runout-active-high")
        args.runout_active_high = False

    if ignored_runout_flags:
        print("WARNING: runout monitoring is disabled; ignoring: " + ", ".join(sorted(set(ignored_runout_flags))))

    if args.version:
        print(VERSION)
        return 0

    if args.doctor:
        run_doctor(args)
        return 0

    if args.self_test:
        run_self_test(args)
        return 0

    if not args.port:
        raise SystemExit("Normal mode requires -p/--port")

    ser = serial.Serial(args.port, args.baud, timeout=0.25)
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
        jam_timeout_adaptive=detection_cfg.get('jam_timeout_adaptive', False),
        jam_timeout_min_s=getattr(args, "jam_timeout_min", 6.0),
        jam_timeout_max_s=getattr(args, "jam_timeout_max", 18.0),
        jam_timeout_k=getattr(args, "jam_timeout_k", 16.0),
        jam_timeout_pps_floor=getattr(args, "jam_timeout_pps_floor", 0.3),
        jam_timeout_ema_halflife_s=getattr(args, "jam_timeout_ema_halflife", 3.0),
        arm_grace_pulses=getattr(args, "arm_grace_pulses", 0),
        arm_grace_s=getattr(args, "arm_grace_s", 0.0),
        arm_min_pulses=args.arm_min_pulses,
        pause_gcode=args.pause_gcode,
        verbose=args.verbose,
        breadcrumb_interval_s=args.breadcrumb_interval,
        pulse_window_s=args.pulse_window,
        stall_thresholds_s=args.stall_thresholds,
        rearm_button_gpio=args.rearm_button_gpio,
        rearm_button_active_high=False,
        rearm_button_debounce_s=args.rearm_button_debounce,
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
        jam_timeout_adaptive=detection_cfg.get('jam_timeout_adaptive', False),
        jam_timeout_min_s=getattr(args, "jam_timeout_min", 6.0),
        jam_timeout_max_s=getattr(args, "jam_timeout_max", 18.0),
        jam_timeout_k=getattr(args, "jam_timeout_k", 16.0),
        jam_timeout_pps_floor=getattr(args, "jam_timeout_pps_floor", 0.3),
        jam_timeout_ema_halflife_s=getattr(args, "jam_timeout_ema_halflife", 3.0),
        arm_grace_pulses=getattr(args, "arm_grace_pulses", 0),
        arm_grace_s=getattr(args, "arm_grace_s", 0.0),
            pause_gcode=args.pause_gcode,
            verbose=args.verbose,
            control_socket=getattr(args, "control_socket", None),
        )

    mon.attach_serial(ser)
    mon.start_serial_reader(verbose=args.verbose)
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
        if t is not None and not t.is_alive():
            logger.emit("serial_thread_dead")
            exit_code = 3
            stop.set()
            break
        time.sleep(0.2)

    mon.stop()
    if getattr(mon, "_serial_thread", None) is not None:
        try:
            mon._serial_thread.join(timeout=1.0)
        except Exception:
            pass
    ser.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())