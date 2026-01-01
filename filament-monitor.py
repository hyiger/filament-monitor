#!/usr/bin/env python3
#
# Filament motion and runout monitor
#
# This program is designed for use with Marlin-based firmware, including
# Marlin-based firmware variants, and relies on firmware serial output behavior
# compatible with those environments.
#
# The monitor observes filament motion and an optional runout signal, and
# issues a pause command when expected motion is not observed.
#
# Control and state transitions are driven by simple text markers received
# over the printer's serial connection (e.g. `filmon:*`).
#

from __future__ import annotations


USAGE_EXAMPLES = """\
Usage examples:
  # Run normally (printer connected over USB)
  python filament-monitor.py -p /dev/ttyACM0

  # Motion + runout inputs (BCM numbering)
  python filament-monitor.py -p /dev/ttyACM0 --motion-gpio 26 --runout-gpio 27 --runout-enabled --runout-active-high

  # Conservative jam tuning
  python filament-monitor.py -p /dev/ttyACM0 --arm-min-pulses 12 --jam-timeout-s 8.0

  # Safe dry-run (does not send pause commands)
  python filament-monitor.py --self-test -p /dev/ttyACM0

  # Host/printer diagnostic
  python filament-monitor.py --doctor -p /dev/ttyACM0
"""

from argparse import RawDescriptionHelpFormatter
import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional

# ---------------- Dependencies ----------------
#
# The monitor can be imported without hardware dependencies so unit tests can run
# on any machine. Hardware-specific imports are required at runtime when GPIO/serial
# features are actually used.

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    serial = None

try:
    from gpiozero import DigitalInputDevice, Device
    from gpiozero.pins.lgpio import LGPIOFactory
    # Force lgpio backend (Pi 5 / Debian Trixie+)
    Device.pin_factory = LGPIOFactory()
except ImportError:  # pragma: no cover
    Device = None
    LGPIOFactory = None

    class DigitalInputDevice:  # minimal stub for non-hardware unit tests
        """GPIO stub used when gpiozero is not installed.

        The real implementation is only required when running on hardware.
        """
        def __init__(self, *args, **kwargs):
            self.when_activated = None
            self.when_deactivated = None

VERSION = "1.0.4"

CONTROL_ENABLE  = "filmon:enable"
CONTROL_DISABLE = "filmon:disable"
CONTROL_RESET   = "filmon:reset"

def now_s() -> float:
    """Return current monotonic time in seconds (float). Used for timeout math."""
    return time.monotonic()

@dataclass
class MonitorState:
    """Holds mutable runtime state for the monitor.

    This is the shared state updated by GPIO callbacks, the serial reader, and the
    main monitoring loop. It includes arming/enabled/latch flags and timing/pulse
    counters used for jam and runout decisions."""
    enabled: bool = False
    armed: bool = False
    latched: bool = False
    pause_sent_ts: float = 0.0
    last_trigger: str = ""
    last_trigger_ts: float = 0.0

    motion_pulses_total: int = 0
    motion_pulses_since_reset: int = 0
    last_pulse_ts: float = 0.0

    runout_asserted: bool = False
    serial_connected: bool = False
    serial_port: str = ""
    baud: int = 0

class JsonLogger:
    """Minimal structured logger.

    Emits single-line JSON events for state transitions (arming, jam, runout, pause)
    so logs are easy to grep and machine-parse."""
    def __init__(self, enable_json: bool):
        """Create a JSON logger.

        Args:
            stream: A file-like object (defaults to stdout) used for event output.
        """
        self.enable_json = enable_json

    def emit(self, event: str, **fields):
        """Emit a JSON event with a name and optional key/value fields."""
        payload = {"ts": time.time(), "event": event, **fields}
        if self.enable_json:
            print(json.dumps(payload, sort_keys=True), flush=True)
        else:
            msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {event}"
            if fields:
                msg += " " + " ".join(f"{k}={v}" for k, v in fields.items())
            print(msg, flush=True)

class SerialThread(threading.Thread):
    """Background serial reader.

    Continuously reads lines from the printer's serial port and forwards them to the
    monitor for control-marker handling and (optional) diagnostics."""
    def __init__(self, ser, out_q, stop_evt, logger, verbose: bool = False):
        """Create the serial reader thread.

        Args:
            ser: An open pyserial Serial instance.
            on_line: Callback invoked with each decoded line (str).
            logger: JsonLogger for emitting serial-related events (optional).
        """
        super().__init__(daemon=True)
        self.ser = ser
        self.out_q = out_q
        self.stop_evt = stop_evt
        self.logger = logger
        self.verbose = bool(verbose)

    def run(self):
        """Thread entry point. Reads serial lines until stopped."""
        while not self.stop_evt.is_set():
            try:
                line = self.ser.readline()
                if not line:
                    continue
                text = line.decode("utf-8", errors="replace").strip()
                self.out_q.put(text)
            except Exception as e:
                try:
                    self.logger.emit("serial_read_error", error=str(e))
                except Exception:
                    pass
                break

class FilamentMonitor:
    """Filament motion/runout monitor controller.

    Wires together GPIO edge callbacks, serial control markers (filmon:enable|disable|reset), and the
    jam/runout decision logic. When a fault is detected while enabled, it sends a
    pause command (default: M600) over serial and latches until reset."""
    def __init__(
        self,
        state: MonitorState,
        logger: JsonLogger,
        motion_gpio: int,
        runout_gpio: Optional[int],
        runout_active_high: bool,
        runout_debounce_s: float,
        jam_timeout_s: float,
        arm_min_pulses: int,
        pause_gcode: str,
        verbose: bool = False,
    ):
        """
        Initialize the monitor.
        
        Sets up GPIO inputs, thresholds, and serial control handling.
        Threads are started by start(); construction is side-effect free.
        """
        self.state = state
        self.logger = logger
        self.verbose = bool(verbose)

        self.jam_timeout_s = jam_timeout_s
        self.arm_min_pulses = arm_min_pulses
        self.pause_gcode = pause_gcode.strip()

        self.motion = DigitalInputDevice(motion_gpio, pull_up=True)
        self.motion.when_deactivated = self._on_motion_pulse

        self.runout = None
        self.runout_active_high = runout_active_high
        self.runout_debounce_s = runout_debounce_s
        self._last_runout_edge = 0.0

        if runout_gpio is not None:
            self.runout = DigitalInputDevice(runout_gpio, pull_up=True)
            if runout_active_high:
                self.runout.when_activated   = self._on_runout_asserted
                self.runout.when_deactivated = self._on_runout_cleared
            else:
                self.runout.when_deactivated = self._on_runout_asserted
                self.runout.when_activated   = self._on_runout_cleared

        self._ser = None
        self._stop_evt = threading.Event()
        self._serial_q = queue.Queue()
        self._serial_thread = None

    def _on_motion_pulse(self):
        """GPIO callback for filament-motion pulses.

        Updates pulse counters and timestamps used to determine whether filament is
        moving when the monitor is enabled."""
        ts = now_s()
        self.state.motion_pulses_total += 1
        self.state.motion_pulses_since_reset += 1
        self.state.last_pulse_ts = ts


        if self.state.enabled and not self.state.armed:
            if self.state.motion_pulses_since_reset >= self.arm_min_pulses:
                # If we are coming back from a prior pause (latched), allow a new event
                # after credible motion resumes.
                if self.state.latched and (time.time() - self.state.pause_sent_ts) > 2.0:
                    self.state.latched = False
                    self.logger.emit("auto_unlatched")
                # Only arm when not latched (or after auto-unlatch above).
                if not self.state.latched:
                    self.state.armed = True
                    self.logger.emit("armed")


    def _debounced(self) -> bool:
        """Return True if the runout input change passes debounce filtering."""
        ts = now_s()
        if ts - self._last_runout_edge < self.runout_debounce_s:
            return False
        self._last_runout_edge = ts
        return True

    def _on_runout_asserted(self):
        """GPIO callback when the runout switch asserts (filament not present)."""
        if not self._debounced():
            return
        # Always track the debounced runout state, but only log/act once armed.
        self.state.runout_asserted = True
        if self.state.armed:
            self.logger.emit("runout_asserted")
            if self.state.enabled and not self.state.latched:
                self._trigger_pause("runout")

    def _on_runout_cleared(self):
        """GPIO callback when the runout switch clears (filament present)."""
        if not self._debounced():
            return
        # Always track the debounced runout state, but only log once armed.
        self.state.runout_asserted = False
        if self.state.armed:
            self.logger.emit("runout_cleared")

    def attach_serial(self, ser):
        """Attach an already-open serial port to the monitor."""
        self._ser = ser

    def start_serial_reader(self, verbose: bool = False):
        """Start the background serial reader thread if a serial port is attached."""
        t = SerialThread(self._ser, self._serial_q, self._stop_evt, self.logger, verbose=verbose)
        t.start()
        self._serial_thread = t

    def _send_gcode(self, gcode):
        """Send a single G-code line over serial (adds newline and flushes)."""
        self._ser.write((gcode + "\n").encode())
        self._ser.flush()
        self.logger.emit("gcode_sent", gcode=gcode)

    def _trigger_pause(self, reason):
        """Latch and send the pause command due to a detected fault.

        Args:
            reason: Short string describing the fault (e.g. 'jam', 'runout').
        """
        self.state.latched = True
        self.state.pause_sent_ts = time.time()
        self.state.last_trigger = reason
        self.state.last_trigger_ts = time.time()
        self.logger.emit("pause_triggered", reason=reason)
        self._send_gcode(self.pause_gcode)

    def _maybe_jam(self):
        """Evaluate jam condition based on pulse timing and thresholds.

        This is called periodically by the main loop when enabled/armed and not latched."""
        if not self.state.enabled or self.state.latched or not self.state.armed:
            return
        if now_s() - self.state.last_pulse_ts >= self.jam_timeout_s:
            self._trigger_pause("jam")

    def _handle_control_marker(self, line):
        """Handle a decoded control marker.

        Supported markers:
            filmon:enable   - enable monitoring
            filmon:disable  - disable monitoring
            filmon:reset    - clear latch/arming and counters
        """
        low = line.lower()
        if CONTROL_ENABLE in low:
            self.state.enabled = True
            self.state.motion_pulses_since_reset = 0
            self.state.armed = False
            self.logger.emit("enabled")
        elif CONTROL_DISABLE in low:
            self.state.enabled = False
            self.state.armed = False
            self.logger.emit("disabled")
        elif CONTROL_RESET in low:
            self.state.latched = False
            self.state.armed = False
            self.state.motion_pulses_since_reset = 0
            self.logger.emit("reset")

    def start(self):
        """Start GPIO monitoring and the main loop (and serial reader if configured)."""
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        """Stop threads and clean up GPIO/serial resources."""
        self._stop_evt.set()

    def _loop(self):
        """Main periodic loop. Arms on sufficient pulses and checks for jam/runout faults."""
        while not self._stop_evt.is_set():
            try:
                line = self._serial_q.get(timeout=0.2)
                if self.verbose:
                    self.logger.emit("serial", line=line)
                self._handle_control_marker(line)
            except queue.Empty:
                pass
            self._maybe_jam()

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

def build_arg_parser():
    """Construct the CLI argument parser for the daemon."""
    ap = argparse.ArgumentParser(epilog=USAGE_EXAMPLES, formatter_class=RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", help="Serial device for the printer connection (e.g., /dev/ttyACM0).")
    ap.add_argument("--baud", type=int, default=115200, help="Serial baud rate for the printer connection (default: 115200).")
    ap.add_argument("--motion-gpio", type=int, default=26, help="BCM GPIO pin number for the filament motion pulse input.")
    ap.add_argument("--runout-gpio", type=int, default=27, help="BCM GPIO pin number for the optional runout input.")
    ap.add_argument("--runout-enabled", action="store_true", default=False, help="Enable runout monitoring (default: disabled).")
    ap.add_argument("--runout-debounce", type=float, default=None, help="Debounce time (seconds) applied to the runout input to ignore short glitches.")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging (includes serial chatter).")
    ap.add_argument("--no-banner", action="store_true", help="Disable the startup banner.")
    ap.add_argument("--runout-active-high", action="store_true", default=False, help="Treat the runout signal as active-high (default is active-low).")
    ap.add_argument("--doctor", action="store_true", help="Run host/printer diagnostics (GPIO + serial checks) and exit.")
    ap.add_argument("--self-test", action="store_true", help="Dry-run mode: monitor inputs and parsing but do not send pause commands.")
    ap.add_argument("--pause-gcode", default="M600", help="G-code to send when a jam/runout is detected (default: M600).")
    ap.add_argument("--jam-timeout", type=float, default=8.0, help="Seconds without motion pulses (after arming) before declaring a jam (default: 8.0).")
    ap.add_argument("--arm-min-pulses", type=int, default=12, help="Minimum motion pulses required before jam detection is armed.")
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

def main():
    """CLI entry point. Parses args, configures the monitor, and starts the daemon."""
    ap = build_arg_parser()
    if len(sys.argv) == 1:
        ap.print_help()
        return 0

    args = ap.parse_args()

    # Runout option guardrails
    # - Runout monitoring is disabled by default.
    # - --runout-gpio defaults to 27 and is only used when runout is enabled.
    # - Runout debounce and polarity only apply when runout is enabled.

    ignored_runout_flags = []
    if getattr(args, "runout_gpio", None) is not None and not getattr(args, "runout_enabled", False):
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
    logger = JsonLogger(enable_json=False)
    mon = FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=args.motion_gpio,
        runout_gpio=(args.runout_gpio if args.runout_enabled else None),
        runout_active_high=args.runout_active_high,
        runout_debounce_s=args.runout_debounce,
        jam_timeout_s=args.jam_timeout,
        arm_min_pulses=args.arm_min_pulses,
        pause_gcode=args.pause_gcode,
        verbose=args.verbose,
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
            pause_gcode=args.pause_gcode,
            verbose=args.verbose,
        )

    mon.attach_serial(ser)
    mon.start_serial_reader(verbose=args.verbose)
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
