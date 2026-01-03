from __future__ import annotations

import json
import collections
import queue
import socket
import threading
import time
from typing import Optional

from .gpio import DigitalInputDevice
from .logging import JsonLogger
from .serialio import SerialThread
from .state import MonitorState
from .util import now_s
from .constants import CONTROL_ENABLE, CONTROL_DISABLE, CONTROL_RESET, CONTROL_ARM, CONTROL_UNARM, VERSION
import os

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


