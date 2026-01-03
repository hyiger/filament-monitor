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

  # Conservative jam tuning (marker-driven arming)
  python filament-monitor.py -p /dev/ttyACM0 --jam-timeout 8 --stall-thresholds 3,6 --verbose --json

  # Safe dry-run (does not send pause commands)
  python filament-monitor.py --self-test -p /dev/ttyACM0

  # Host/printer diagnostic
  python filament-monitor.py --doctor -p /dev/ttyACM0
"""

from argparse import RawDescriptionHelpFormatter
import argparse
import json
import collections
import os
import queue
import signal
import sys
import threading
import time
import json
import socket
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

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
CONTROL_ARM     = "filmon:arm"
CONTROL_UNARM   = "filmon:unarm"


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

    motion_pulses_since_arm: int = 0
    arm_ts: float = 0.0

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
        t = time.time()
        # ts: float seconds since epoch (sub-second resolution). ts_iso is a human-friendly local timestamp with milliseconds.
        ts_iso = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t)) + f'.{int((t - int(t))*1000):03d}'
        payload = {"ts": t, "ts_iso": ts_iso, "event": event, **fields}
        if self.enable_json:
            print(json.dumps(payload, sort_keys=True), flush=True)
        else:
            t = time.time()
            ms = int((t - int(t)) * 1000)
            msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))}.{ms:03d}] {event}"
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
        breadcrumb_interval_s: float = 2.0,
        pulse_window_s: float = 2.0,
        stall_thresholds_s: Optional[str] = "3,6",
        rearm_button_gpio: Optional[int] = None,
        rearm_button_active_high: bool = False,
        rearm_button_debounce_s: float = 0.25,
        rearm_button_long_press_s: float = 1.5,
    ):
        """
        Initialize the monitor.

        Sets up GPIO inputs, thresholds, and serial control handling.
        Threads are started by start(); construction is side-effect free.
        """
        self.state = state
        self.logger = logger
        self.verbose = bool(verbose)


        # Pulse breadcrumb / rate tracking
        self._pulse_times = collections.deque()  # monotonic timestamps of recent pulses
        self._pulse_window_s = float(pulse_window_s)
        self._breadcrumb_interval_s = float(breadcrumb_interval_s)
        # thresholds (seconds since last pulse) at which we emit 'stall' breadcrumbs while armed
        self._stall_thresholds_s = []
        try:
            if stall_thresholds_s:
                self._stall_thresholds_s = sorted({float(x.strip()) for x in str(stall_thresholds_s).split(",") if x.strip()})
        except Exception:
            self._stall_thresholds_s = [3.0, 6.0]
        self._stall_next_idx = 0
        self._next_hb_ts = now_s() + self._breadcrumb_interval_s
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


        # Optional physical "rearm" button. Lets you re-arm without sharing the
        # printer serial port (useful when this daemon owns /dev/ttyACM0).
        self.rearm_button = None
        self.rearm_button_active_high = bool(rearm_button_active_high)
        self.rearm_button_debounce_s = float(rearm_button_debounce_s)
        self.rearm_button_long_press_s = float(rearm_button_long_press_s)
        self._last_rearm_edge = 0.0
        self._rearm_press_start_ts: float | None = None

        if rearm_button_gpio is not None:
            # Optional physical button.
            #
            # Active-low (recommended): enable pull-up, press shorts to GND.
            # Active-high: enable pull-down (pull_up=False), press drives pin high.
            pull_up = not self.rearm_button_active_high
            self.rearm_button = DigitalInputDevice(rearm_button_gpio, pull_up=pull_up)

            if self.rearm_button_active_high:
                # press = activated (high), release = deactivated
                self.rearm_button.when_activated = self._on_rearm_button_press
                self.rearm_button.when_deactivated = self._on_rearm_button_release
            else:
                # press = deactivated (low), release = activated
                self.rearm_button.when_deactivated = self._on_rearm_button_press
                self.rearm_button.when_activated = self._on_rearm_button_release


        self._ser = None
        self._ser_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._serial_q = queue.Queue()
        self._serial_thread = None

        # Optional local control socket (lets you re-arm without sharing the printer serial port)
        self._control_thread = None
        self._control_stop_evt = threading.Event()
        self._control_sock_path: Optional[str] = None


    def _on_rearm_button_press(self):
        """GPIO callback for the optional physical button *press*.

        We debounce on the press edge and record the press start time. The action
        (reset vs rearm) is chosen on release based on the press duration.
        """
        if self._stop_evt.is_set():
            return
        now = now_s()
        if (now - self._last_rearm_edge) < self.rearm_button_debounce_s:
            return
        self._last_rearm_edge = now
        self._rearm_press_start_ts = now

    def _on_rearm_button_release(self):
        """GPIO callback for the optional physical button *release*.

        Short press => reset (same semantics as `filmon:reset`).
        Long press  => rearm (clear latch and arm detection).
        """
        if self._stop_evt.is_set():
            return
        if self._rearm_press_start_ts is None:
            return
        now = now_s()
        dur = now - self._rearm_press_start_ts
        self._rearm_press_start_ts = None

        if dur >= self.rearm_button_long_press_s:
            # Long press: rearm (clears latch and arms)
            self._cmd_rearm()
        else:
            # Short press: reset (clears latch/counters and disables monitoring)
            self._handle_control_marker(CONTROL_RESET)

    def _on_motion_pulse(self):
        """GPIO callback for filament-motion pulses.

        Updates pulse counters and timestamps used to determine whether filament is
        moving when the monitor is enabled."""
        # Ignore late GPIO callbacks once shutdown begins.
        if self._stop_evt.is_set():
            return

        ts = now_s()
        # Track recent pulses for pps breadcrumbs.
        self._pulse_times.append(ts)
        self._prune_pulses(ts)

        self.state.motion_pulses_total += 1
        self.state.motion_pulses_since_reset += 1

        # Per-arm pulse counter + first-pulse breadcrumb
        if self.state.armed:
            if self.state.motion_pulses_since_arm == 0 and self.state.arm_ts:
                self.logger.emit("first_pulse_after_arm", dt=round(ts - self.state.arm_ts, 3))
            self.state.motion_pulses_since_arm += 1

        self.state.last_pulse_ts = ts
        # New pulse resets stall breadcrumb progression.
        self._stall_next_idx = 0


    def _prune_pulses(self, now: float):
        """Drop pulse timestamps older than the configured window."""
        if self._pulse_window_s <= 0:
            self._pulse_times.clear()
            return
        cutoff = now - self._pulse_window_s
        while self._pulse_times and self._pulse_times[0] < cutoff:
            self._pulse_times.popleft()

    def _pps(self, now: float) -> float:
        """Return pulses-per-second over the recent window."""
        self._prune_pulses(now)
        if self._pulse_window_s <= 0:
            return 0.0
        return float(len(self._pulse_times)) / float(self._pulse_window_s)

    def _reset_pulse_tracking(self):
        """Reset pulse-rate tracking and stall breadcrumb state."""
        self._pulse_times.clear()
        self._stall_next_idx = 0
        self._next_hb_ts = now_s() + self._breadcrumb_interval_s

    def _maybe_breadcrumbs(self):
        """Emit low-volume 'heartbeat' and 'stall' breadcrumbs for debugging/tuning."""
        now = now_s()

        # Heartbeat snapshot (enabled only, to avoid noise when fully off)
        if self._breadcrumb_interval_s > 0 and self.state.enabled and now >= self._next_hb_ts:
            dt = now - self.state.last_pulse_ts if self.state.last_pulse_ts else None
            self.logger.emit(
                "hb",
                enabled=int(self.state.enabled),
                armed=int(self.state.armed),
                latched=int(self.state.latched),
                runout=int(self.state.runout_asserted),
                dt_since_pulse=(round(dt, 3) if dt is not None else None),
                pps=round(self._pps(now), 3),
                pulses_reset=self.state.motion_pulses_since_reset,
                pulses_arm=self.state.motion_pulses_since_arm,
            )
            self._next_hb_ts = now + self._breadcrumb_interval_s

        # Stall breadcrumbs: only while detection is active
        if not (self.state.enabled and self.state.armed) or self.state.latched:
            return

        if not self._stall_thresholds_s:
            return

        dt = now - self.state.last_pulse_ts
        while self._stall_next_idx < len(self._stall_thresholds_s) and dt >= self._stall_thresholds_s[self._stall_next_idx]:
            thr = self._stall_thresholds_s[self._stall_next_idx]
            self.logger.emit(
                "stall",
                dt_since_pulse=round(dt, 3),
                threshold_s=thr,
                pps=round(self._pps(now), 3),
                pulses_arm=self.state.motion_pulses_since_arm,
            )
            self._stall_next_idx += 1

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

    # ---------------- Local control socket ----------------
    # The monitor holds the printer serial port, so external consoles cannot
    # concurrently send G-code. A local UNIX socket provides a safe control plane
    # (e.g. "rearm" after a jam) without serial-port sharing.

    def start_control_socket(self, sock_path: str):
        """Start a local control socket.

        The socket accepts single-line commands and returns a single-line JSON response.
        Supported commands: status, rearm, reset, enable, arm, unarm, disable.
        """
        if not sock_path:
            return
        self._control_sock_path = sock_path
        t = threading.Thread(target=self._control_loop, daemon=True)
        t.start()
        self._control_thread = t
        self.logger.emit("control_socket_started", path=sock_path)

    def _control_loop(self):
        path = self._control_sock_path
        if not path:
            return

        # Ensure parent directory exists (useful with RuntimeDirectory=/run/filmon)
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        except Exception:
            pass

        # Ensure any stale socket is removed.
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(path)
            # Restrict to local users. systemd can further manage permissions via RuntimeDirectory.
            try:
                os.chmod(path, 0o660)
            except Exception:
                pass
            srv.listen(4)
            srv.settimeout(0.5)
        except Exception as e:
            try:
                self.logger.emit("control_socket_error", error=str(e), path=path)
            except Exception:
                pass
            try:
                srv.close()
            except Exception:
                pass
            return

        while not self._stop_evt.is_set() and not self._control_stop_evt.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            try:
                conn.settimeout(2.0)
                data = b""
                while b"\n" not in data and len(data) < 4096:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                cmd = data.decode("utf-8", errors="replace").strip()
                resp = self._handle_control_command(cmd)
                conn.sendall((json.dumps(resp, sort_keys=True) + "\n").encode("utf-8"))
            except Exception as e:
                try:
                    conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode("utf-8"))
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        try:
            srv.close()
        except Exception:
            pass
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _handle_control_command(self, cmd: str) -> dict:
        cmd = (cmd or "").strip().lower()
        if not cmd:
            return {"ok": False, "error": "empty command"}

        if cmd in ("status", "state"):
            return {"ok": True, "state": asdict(self.state), "version": VERSION}

        if cmd == "rearm":
            self._cmd_rearm()
            return {"ok": True}

        # Map simple state transitions to the same semantics as serial markers.
        if cmd == "reset":
            self._handle_control_marker(CONTROL_RESET)
            return {"ok": True}
        if cmd == "enable":
            self._handle_control_marker(CONTROL_ENABLE)
            return {"ok": True}
        if cmd == "arm":
            self._handle_control_marker(CONTROL_ARM)
            return {"ok": True}
        if cmd == "unarm":
            self._handle_control_marker(CONTROL_UNARM)
            return {"ok": True}
        if cmd == "disable":
            self._handle_control_marker(CONTROL_DISABLE)
            return {"ok": True}

        return {"ok": False, "error": f"unknown command: {cmd}"}

    def _cmd_rearm(self):
        """Clear a latched pause and re-arm detection.

        Intended to be used after the operator clears a jam and is about to resume
        the print. This does not require a second serial connection.
        """
        # Clear latch and counters, then arm with a fresh timeout reference.
        self.state.latched = False
        self.state.runout_asserted = False
        self.state.motion_pulses_since_reset = 0
        self.state.motion_pulses_since_arm = 0
        now = now_s()
        self.state.arm_ts = now
        self.state.last_pulse_ts = now
        self._reset_pulse_tracking()
        self._stall_next_idx = 0
        self.state.enabled = True
        self.state.armed = True
        self.logger.emit("rearmed")

    def _send_gcode(self, gcode):
        """Send a single G-code line over serial (adds newline and flushes)."""
        # Serial can be written from the main loop and GPIO callbacks.
        # Keep writes atomic to avoid interleaving lines.
        with self._ser_lock:
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
        now = now_s()
        dt = (now - self.state.last_pulse_ts) if self.state.last_pulse_ts else None
        self.logger.emit(
            "pause_triggered",
            reason=reason,
            dt_since_pulse=(round(dt, 3) if dt is not None else None),
            pps=round(self._pps(now), 3),
            pulses_reset=self.state.motion_pulses_since_reset,
            pulses_arm=self.state.motion_pulses_since_arm,
        )
        # Ensure the planner is drained before pausing.
        self._send_gcode("M400")
        self._send_gcode(self.pause_gcode)

    def _maybe_jam(self):
        """Evaluate jam condition based on pulse timing and thresholds.

        This is called periodically by the main loop. Jams can only trigger when explicitly armed and not latched."""
        if not self.state.enabled or self.state.latched or not self.state.armed:
            return
        if now_s() - self.state.last_pulse_ts >= self.jam_timeout_s:
            self._trigger_pause("jam")
    def _handle_control_marker(self, line):
        """Handle a decoded control marker.

        Markers are the only control plane for arming/pausing decisions. Jam/runout
        detection is *only* active when explicitly armed via `filmon:arm`.

        Supported markers:
            filmon:reset    - clear latch/counters and DISABLE monitoring
            filmon:enable   - enable monitoring (unarmed)
            filmon:arm      - enable monitoring and ARM jam/runout detection
            filmon:unarm    - keep enabled but disarm detection
            filmon:disable  - disable monitoring
        """
        low = line.lower()

        # NOTE: reset always wins.
        if CONTROL_RESET in low:
            self.state.enabled = False
            self.state.armed = False
            self.state.latched = False
            self.state.runout_asserted = False
            self.state.motion_pulses_since_reset = 0
            self.state.last_pulse_ts = now_s()
            self.state.motion_pulses_since_arm = 0
            self.state.arm_ts = 0.0
            self._reset_pulse_tracking()
            self.logger.emit("reset")
            return

        # Ignore state transitions while latched except disable/reset.
        if self.state.latched:
            if CONTROL_DISABLE in low:
                self.state.enabled = False
                self.state.armed = False
                self.logger.emit("disabled")
            return

        if CONTROL_DISABLE in low:
            self.state.enabled = False
            self.state.armed = False
            self.logger.emit("disabled")
            return

        if CONTROL_UNARM in low:
            # Idempotent: unarming should not reset counters.
            self.state.enabled = True
            self.state.armed = False
            self._stall_next_idx = 0
            self.logger.emit("unarmed")
            return

        if CONTROL_ARM in low:
            # Arm implies enabled. Start timeout reference at arm time to avoid an immediate jam.
            self.state.enabled = True
            self.state.armed = True
            self.state.motion_pulses_since_arm = 0
            self.state.arm_ts = now_s()
            self.state.last_pulse_ts = self.state.arm_ts
            self._stall_next_idx = 0
            self.logger.emit("armed")
            return

        if CONTROL_ENABLE in low:
            # Enable only; never arms automatically. Idempotent and does not reset counters.
            if self.state.enabled and not self.state.armed:
                self.logger.emit("enabled")
                return
            self.state.enabled = True
            self.state.armed = False
            self.state.last_pulse_ts = now_s()
            self._stall_next_idx = 0
            self.logger.emit("enabled")
            return

    def start(self):
        """Start GPIO monitoring and the main loop (and serial reader if configured)."""
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        """Stop threads and clean up GPIO/serial resources."""
        self._stop_evt.set()
        self._control_stop_evt.set()

    def _loop(self):
        """Main periodic loop. Processes control markers and checks for jam/runout faults."""
        while not self._stop_evt.is_set():
            try:
                line = self._serial_q.get(timeout=0.2)
                if self.verbose:
                    self.logger.emit("serial", line=line)
                self._handle_control_marker(line)
            except queue.Empty:
                pass
            self._maybe_jam()
            self._maybe_breadcrumbs()


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
        return tomllib.load(f)


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


def main():
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
