from __future__ import annotations

import json
import collections
import queue
import socket
import threading
import math
import time
from typing import Optional

try:
    import serial  # pyserial (only used for exception types in _send_gcode)
except ImportError:  # pragma: no cover
    serial = None


from dataclasses import asdict
from .gpio import DigitalInputDevice
from .logging import JsonLogger
from .serialio import SerialThread
from .state import MonitorState, MonitorMode
from .util import now_s
from .constants import CONTROL_ENABLE, CONTROL_DISABLE, CONTROL_RESET, CONTROL_ARM, CONTROL_UNARM, VERSION
from .notify import Notifier
import os

# Maximum duration treated as release contact bounce. Deliberately much
# shorter than the press-edge debounce default (0.25 s): mechanical bounce is
# single-digit milliseconds, while a real human tap is ~0.1 s and must still
# register as a short press.
REARM_RELEASE_BOUNCE_S = 0.05

# Seconds between retries of an undelivered pause while latched.
PAUSE_RETRY_INTERVAL_S = 5.0

# Exceptions that mean a serial write failed (wedged/closed port, write
# timeout, or no port attached at all).
if serial is not None:
    _SERIAL_WRITE_ERRORS = (serial.SerialException, OSError, AttributeError)
else:  # pragma: no cover
    _SERIAL_WRITE_ERRORS = (OSError, AttributeError)


class FilamentMonitor:
    """Filament motion/runout monitor controller.

    Wires together GPIO edge callbacks, serial control markers, and the
    jam/runout decision logic. When a fault is detected while armed, it sends a
    pause command (default: M600) over serial and latches until reset or rearm."""

    # Class-level fallbacks so minimal instances built via __new__ in tests
    # keep working; __init__ replaces these with per-instance values.
    _state_lock = threading.RLock()
    _pause_delivered = False
    _pause_last_attempt_ts = 0.0
    _pause_retry_interval_s = PAUSE_RETRY_INTERVAL_S

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
        # Adaptive jam timeout (optional; config-only)
        jam_timeout_adaptive: bool = False,
        jam_timeout_min_s: float = 6.0,
        jam_timeout_max_s: float = 18.0,
        jam_timeout_k: float = 16.0,
        jam_timeout_pps_floor: float = 0.3,
        jam_timeout_ema_halflife_s: float = 3.0,
        # Post-(re)arm grace period (optional; config-only)
        arm_grace_pulses: int = 0,
        arm_grace_s: float = 0.0,
        # Testing hook: provide a GPIO module/factory with a DigitalInputDevice
        # attribute to avoid touching real hardware in unit tests.
        gpio_factory=None,
    ):
        """
        Initialize the monitor.

        Sets up GPIO inputs, thresholds, and serial control handling.
        Threads are started by start(); construction is side-effect free.
        """
        self.state = state
        self.logger = logger
        self.verbose = bool(verbose)

        # Allow tests to provide a stub GPIO factory (with DigitalInputDevice)
        # so the monitor can be constructed without claiming real pins.
        self._DigitalInputDevice = getattr(gpio_factory, "DigitalInputDevice", None) if gpio_factory else None
        if self._DigitalInputDevice is None:
            self._DigitalInputDevice = DigitalInputDevice


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
        self.jam_timeout_adaptive = bool(jam_timeout_adaptive)
        self.jam_timeout_min_s = float(jam_timeout_min_s)
        self.jam_timeout_max_s = float(jam_timeout_max_s)
        self.jam_timeout_k = float(jam_timeout_k)
        self.jam_timeout_pps_floor = float(jam_timeout_pps_floor)
        self.jam_timeout_ema_halflife_s = float(jam_timeout_ema_halflife_s)
        self.arm_grace_pulses = int(arm_grace_pulses)
        self.arm_grace_s = float(arm_grace_s)

        self._pps_ema = 0.0
        self._pps_ema_last_ts = 0.0
        self.pause_gcode = pause_gcode.strip()

        self.motion = self._DigitalInputDevice(motion_gpio, pull_up=True)
        self.motion.when_deactivated = self._on_motion_pulse

        self.runout = None
        self.runout_active_high = runout_active_high
        self.runout_debounce_s = runout_debounce_s
        self.state.jam_timeout_adaptive = jam_timeout_adaptive
        self._last_runout_edge = 0.0
        # Timestamp of the last *observed* runout edge, accepted or rejected.
        # Reconciliation keys its quiet-period check on this: a rejected final
        # edge milliseconds ago must not count as a settled input.
        self._last_runout_edge_seen = 0.0

        if runout_gpio is not None:
            # Let gpiozero normalize polarity: with pull_up=not active_high,
            # "active" always means the switch is asserted (filament absent),
            # so the callbacks map unconditionally for both wirings.
            self.runout = self._DigitalInputDevice(runout_gpio, pull_up=not runout_active_high)
            self.runout.when_activated   = self._on_runout_asserted
            self.runout.when_deactivated = self._on_runout_cleared


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
            # With pull_up=not active_high, gpiozero's "active" means pressed for
            # both wirings, so the callbacks map unconditionally.
            pull_up = not self.rearm_button_active_high
            self.rearm_button = self._DigitalInputDevice(rearm_button_gpio, pull_up=pull_up)
            self.rearm_button.when_activated = self._on_rearm_button_press
            self.rearm_button.when_deactivated = self._on_rearm_button_release


        self._ser = None
        self._ser_lock = threading.Lock()
        # Serializes state transitions across the main loop, runout/button
        # callbacks, and the control socket thread. RLock because _maybe_jam
        # calls _trigger_pause while holding it. Motion-pulse callbacks stay
        # lock-free by design (append-only deque + independent counters).
        self._state_lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._serial_q = queue.Queue()
        self._serial_thread = None
        self._loop_thread = None

        # Pause delivery tracking: latch first, then send; if the send fails
        # the main loop retries until it lands (unlimited — safety daemon).
        self._pause_delivered = False
        self._pause_last_attempt_ts = 0.0
        self._pause_retry_interval_s = PAUSE_RETRY_INTERVAL_S

        # Optional local control socket (lets you re-arm without sharing the printer serial port)
        self._control_thread = None
        self._control_stop_evt = threading.Event()
        self._control_sock_path: Optional[str] = None

        # Optional push notifications (Pushover). Off by default.
        notify_enabled = os.getenv("FILMON_NOTIFY", "0") == "1"
        self.notifier = Notifier(
            enabled=notify_enabled,
            pushover_token=os.getenv("PUSHOVER_TOKEN"),
            pushover_user=os.getenv("PUSHOVER_USER"),
            logger=self.logger,
        )


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
        # Contact bounce right after the press shows up as an ultra-short
        # release. Ignore it and keep the press timestamp: the bounce press
        # that follows is rejected by the press-edge debounce, so the real
        # release still measures from the original press. The threshold is
        # capped well below the press debounce so quick taps still register.
        if dur < min(self.rearm_button_debounce_s, REARM_RELEASE_BOUNCE_S):
            return
        self._rearm_press_start_ts = None

        if dur >= self.rearm_button_long_press_s:
            # Long press: rearm (clears latch and arms)
            self._cmd_rearm()
        else:
            # Short press: reset (clears latch/counters and disables monitoring)
            self._handle_control_marker(CONTROL_RESET)

    def _on_motion_pulse(self):
        """GPIO callback for filament-motion pulses.

        Updates pulse counters and timestamps used by jam detection.
        The per-arm counter and first-pulse breadcrumb are only updated while ARMED."""
        # Ignore late GPIO callbacks once shutdown begins.
        if self._stop_evt.is_set():
            return

        ts = now_s()
        # Track recent pulses for pps breadcrumbs. Append-only here: pruning
        # happens on the main loop (via _pps), so this callback never races a
        # concurrent prune/clear on the deque.
        self._pulse_times.append(ts)

        self.state.motion_pulses_total += 1
        self.state.motion_pulses_since_reset += 1

        # Per-arm pulse counter + first-pulse breadcrumb
        if self.state.mode == MonitorMode.ARMED:
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
        try:
            while self._pulse_times and self._pulse_times[0] < cutoff:
                self._pulse_times.popleft()
        except IndexError:
            # A concurrent clear() (reset/rearm from another thread) emptied
            # the deque mid-prune; nothing left to drop.
            pass

    def _pps(self, now: float) -> float:
        """Return pulses-per-second over the recent window."""
        self._prune_pulses(now)
        if self._pulse_window_s <= 0:
            return 0.0
        return float(len(self._pulse_times)) / float(self._pulse_window_s)

    def _update_pps_ema(self, now: float) -> float:
        """Update and return an EMA of pulses-per-second.

        The EMA is used for adaptive jam timeout. If jam_timeout_ema_halflife_s <= 0,
        the EMA tracks the instantaneous pps.
        """
        pps_now = self._pps(now)
        if self._pps_ema_last_ts <= 0.0:
            self._pps_ema = pps_now
            self._pps_ema_last_ts = now
            return self._pps_ema

        dt = max(0.0, now - self._pps_ema_last_ts)
        self._pps_ema_last_ts = now

        hl = float(self.jam_timeout_ema_halflife_s)
        if hl <= 0.0:
            # Halflife disabled: EMA tracks the instantaneous pps.
            self._pps_ema = pps_now
            return self._pps_ema
        if dt <= 0.0:
            # No time elapsed since the last update (e.g. two calls in the same
            # loop cycle): leave the smoothed EMA unchanged rather than snapping
            # it to the instantaneous pps.
            return self._pps_ema

        tau = hl / math.log(2.0)
        alpha = 1.0 - math.exp(-dt / tau)
        self._pps_ema = (1.0 - alpha) * self._pps_ema + alpha * pps_now
        return self._pps_ema

    def _effective_jam_timeout_from(self, ema: float) -> float:
        """Return the effective jam timeout (seconds) for a given pps EMA value."""
        if not self.jam_timeout_adaptive:
            return float(self.jam_timeout_s)

        denom = max(float(self.jam_timeout_pps_floor), float(ema))
        if denom <= 0.0:
            return float(self.jam_timeout_max_s)

        t = float(self.jam_timeout_k) / denom
        return max(float(self.jam_timeout_min_s), min(float(self.jam_timeout_max_s), t))

    def _effective_jam_timeout_s(self, now: float) -> float:
        """Return the effective jam timeout (seconds), possibly adaptive."""
        if not self.jam_timeout_adaptive:
            return float(self.jam_timeout_s)

        return self._effective_jam_timeout_from(self._update_pps_ema(now))

    def _reset_pulse_tracking(self):
        """Reset pulse-rate tracking and stall breadcrumb state."""
        # The motion callback appends to the deque lock-free; the clear() is
        # serialized under the state lock (callers already hold it — RLock).
        with self._state_lock:
            self._pulse_times.clear()
            self._pps_ema = 0.0
            self._pps_ema_last_ts = 0.0
            self._stall_next_idx = 0
            self._next_hb_ts = now_s() + self._breadcrumb_interval_s

    def _maybe_breadcrumbs(self):
        """Emit low-volume 'heartbeat' and 'stall' breadcrumbs for debugging/tuning."""
        now = now_s()

        # Heartbeat snapshot (enabled only, to avoid noise when fully off)
        if self._breadcrumb_interval_s > 0 and self.state.mode != MonitorMode.DISABLED and now >= self._next_hb_ts:
            dt = now - self.state.last_pulse_ts if self.state.last_pulse_ts else None
            # Single EMA update per heartbeat: the effective timeout is derived
            # from the same value that is logged.
            ema = self._update_pps_ema(now)
            self.logger.emit(
                "hb",
                mode=self.state.mode,
                latched=int(self.state.latched),
                runout=int(self.state.runout_asserted),
                dt_since_pulse=(round(dt, 3) if dt is not None else None),
                pps=round(self._pps(now), 3),
                pps_ema=round(ema, 3),
                jam_timeout_effective_s=round(self._effective_jam_timeout_from(ema), 3),
                pulses_reset=self.state.motion_pulses_since_reset,
                pulses_arm=self.state.motion_pulses_since_arm,
            )
            self._next_hb_ts = now + self._breadcrumb_interval_s

        # Stall breadcrumbs: only while detection is active
        if self.state.mode != MonitorMode.ARMED or self.state.latched:
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
        # Record every observed edge (even rejected ones) so _reconcile_runout
        # never treats a still-chattering input as settled.
        self._last_runout_edge_seen = ts
        if ts - self._last_runout_edge < self.runout_debounce_s:
            return False
        self._last_runout_edge = ts
        return True

    def _on_runout_asserted(self):
        """GPIO callback when the runout switch asserts (filament not present)."""
        # Ignore late GPIO callbacks once shutdown begins.
        if self._stop_evt.is_set():
            return
        if not self._debounced():
            return
        # Always track the debounced runout state, but only log/act once armed.
        with self._state_lock:
            self.state.runout_asserted = True
            if self.state.mode == MonitorMode.ARMED:
                self.logger.emit("runout_asserted")
                if not self.state.latched:
                    self._trigger_pause("runout")

    def _on_runout_cleared(self):
        """GPIO callback when the runout switch clears (filament present)."""
        # Ignore late GPIO callbacks once shutdown begins.
        if self._stop_evt.is_set():
            return
        if not self._debounced():
            return
        # Always track the debounced runout state, but only log once armed.
        with self._state_lock:
            self.state.runout_asserted = False
            if self.state.mode == MonitorMode.ARMED:
                self.logger.emit("runout_cleared")

    def _runout_asserted_now(self) -> bool:
        """Return True if the runout condition currently holds.

        Prefers the live device level (is_active is polarity-normalized by the
        pull_up choice at construction); falls back to the tracked state when
        the device exposes no level (e.g. test stubs). False when runout
        monitoring is disabled.
        """
        if self.runout is None:
            return False
        is_active = getattr(self.runout, "is_active", None)
        if is_active is None:
            return bool(self.state.runout_asserted)
        return bool(is_active)

    def _reconcile_runout(self):
        """Sync tracked runout state to the live pin level once debounce settles.

        Edge callbacks inside the debounce window are discarded, so a chatter
        burst whose final edge is swallowed can leave state.runout_asserted
        stale. Called from the main loop; once the quiet period has elapsed
        since the last edge, re-sample the level and reconcile.
        """
        if self.runout is None:
            return
        is_active = getattr(self.runout, "is_active", None)
        if is_active is None:
            return
        now = now_s()
        if self.runout_debounce_s and (now - self._last_runout_edge_seen) < self.runout_debounce_s:
            return
        # Sample and decide under the state lock: an edge callback or an
        # operator reset must not interleave between the level read and the
        # state update/pause.
        with self._state_lock:
            level = bool(self.runout.is_active)
            if level == self.state.runout_asserted:
                return
            self.state.runout_asserted = level
            if self.state.mode != MonitorMode.ARMED:
                return
            if level:
                self.logger.emit("runout_asserted")
                if not self.state.latched:
                    self._trigger_pause("runout")
            else:
                self.logger.emit("runout_cleared")

    def attach_serial(self, ser):
        """Attach an already-open serial port to the monitor.

        Also used by the serial reader to swap in a reopened port after a
        reconnect, so the swap takes the write lock."""
        with self._ser_lock:
            self._ser = ser

    def start_serial_reader(self, verbose: bool = False, port: str = "", baud: int = 0):
        """Start the background serial reader thread if a serial port is attached.

        Args:
            port: Device path used to reopen the port after a read error.
                  Defaults to state.serial_port.
            baud: Baud rate for the reopen. Defaults to state.baud.
        """
        t = SerialThread(
            self._ser,
            self._serial_q,
            self._stop_evt,
            self.logger,
            verbose=verbose,
            port=port or self.state.serial_port,
            baud=baud or self.state.baud,
            state=self.state,
            on_reconnect=self.attach_serial,
            ser_lock=self._ser_lock,
        )
        t.start()
        self._serial_thread = t

    # ---------------- Local control socket ----------------
    # The monitor holds the printer serial port, so external consoles cannot
    # concurrently send G-code. A local UNIX socket provides a safe control plane
    # (e.g. "rearm" after a jam) without serial-port sharing.

    def start_control_socket(self, sock_path: str):
        """Start a local control socket.

        The socket accepts single-line commands and returns a single-line JSON response.
        Supported commands: status, rearm, reset, enable, arm, unarm, disable, test-notify.
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
                self.logger.emit("control_socket_stopped", path=path, reason="bind_failed")
            except Exception:
                pass
            try:
                srv.close()
            except Exception:
                pass
            return

        # Tolerate transient accept failures (e.g. EMFILE); only give up after
        # several in a row so a blip cannot silently remove the control plane.
        consecutive_accept_errors = 0
        stop_reason = "stop_requested"
        while not self._stop_evt.is_set() and not self._control_stop_evt.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except Exception as e:
                consecutive_accept_errors += 1
                try:
                    self.logger.emit(
                        "control_socket_error",
                        error=str(e),
                        path=path,
                        consecutive_errors=consecutive_accept_errors,
                    )
                except Exception:
                    pass
                if consecutive_accept_errors >= 5:
                    stop_reason = "accept_errors"
                    break
                continue
            consecutive_accept_errors = 0

            try:
                # Total read budget for the whole connection. A per-recv timeout
                # alone resets on every byte, letting a byte-dripping client
                # monopolize this single-threaded handler indefinitely.
                deadline = now_s() + 2.0
                data = b""
                while b"\n" not in data and len(data) < 4096:
                    remaining_s = deadline - now_s()
                    if remaining_s <= 0.0:
                        break
                    conn.settimeout(max(0.05, remaining_s))
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                # Only the first line is the command; ignore any pipelined extras.
                cmd = data.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
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
        try:
            self.logger.emit("control_socket_stopped", path=path, reason=stop_reason)
        except Exception:
            pass

    def _handle_control_command(self, cmd: str) -> dict:
        cmd = (cmd or "").strip().lower()
        if not cmd:
            return {"ok": False, "error": "empty command"}

        if cmd in ("status", "state"):
            return {"ok": True, "state": asdict(self.state), "version": VERSION}

        if cmd == "rearm":
            if self._cmd_rearm():
                return {"ok": True}
            return {"ok": False, "error": "not latched"}

        if cmd == "test-notify":
            # Send via the daemon's own Notifier (its environment, its
            # FILMON_NOTIFY gate) so the test proves what a real alert would do.
            if not self.notifier.enabled:
                return {
                    "ok": False,
                    "enabled": False,
                    "error": "notifier disabled: set FILMON_NOTIFY=1, PUSHOVER_TOKEN and PUSHOVER_USER in the daemon environment",
                }
            self.notifier.send(
                title="Filament Monitor",
                message="Test notification (via daemon)",
                priority=0,
            )
            return {"ok": True, "enabled": True}

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

    def _cmd_rearm(self) -> bool:
        """Clear a latched pause and re-arm detection.

        Intended to be used after the operator clears a jam and is about to resume
        the print. This does not require a second serial connection.

        Only valid while latched (LATCHED → ARMED); rearming from any other state
        is refused so a habitual rearm cannot arm an idle printer into a
        guaranteed jam-pause. Returns True when the rearm was applied.
        """
        with self._state_lock:
            if not self.state.latched:
                self.logger.emit("rearm_ignored", reason="not latched", mode=self.state.mode)
                return False
            # Refresh counters and timestamps first, then flip the enabling flags
            # last: a concurrent _maybe_jam must never observe ARMED/unlatched
            # with stale timing (defense in depth on top of the state lock).
            # Note: runout_asserted is intentionally preserved so a still-standing
            # runout re-pauses below instead of being silently forgotten.
            self.state.motion_pulses_since_reset = 0
            self.state.motion_pulses_since_arm = 0
            now = now_s()
            self.state.arm_ts = now
            self.state.last_pulse_ts = now
            self._reset_pulse_tracking()
            self._stall_next_idx = 0
            self.state.latched = False
            self.state.mode = MonitorMode.ARMED
            self.logger.emit("rearmed")
            # A runout still asserted after the operator intervention must pause again.
            if self._runout_asserted_now():
                self.state.runout_asserted = True
                self.logger.emit("runout_asserted")
                self._trigger_pause("runout")
            return True

    def _send_gcode(self, gcode) -> bool:
        """Send a single G-code line over serial (adds newline and flushes).

        Returns True when the write reached the port, False when it failed
        (wedged/closed port, write timeout, or no port attached). Failures are
        logged as 'gcode_send_failed' instead of raising so callers can retry.
        """
        try:
            # Serial can be written from the main loop and GPIO callbacks.
            # Keep writes atomic to avoid interleaving lines.
            #
            # No flush(): write() is bounded by write_timeout, but flush()
            # (tcdrain) has no timeout and can block the monitor loop forever
            # while a wedged printer stops draining its CDC buffer — the exact
            # case the pause-retry path exists for. The OS transmits the
            # buffered bytes asynchronously.
            with self._ser_lock:
                self._ser.write((gcode + "\n").encode())
        except _SERIAL_WRITE_ERRORS as e:
            self.logger.emit("gcode_send_failed", gcode=gcode, error=str(e))
            return False
        self.logger.emit("gcode_sent", gcode=gcode)
        return True

    def _send_pause_gcode(self) -> bool:
        """Send the pause sequence (best-effort M400 drain, then pause G-code).

        Delivery is judged by the pause line ALONE: retrying because only the
        M400 failed would queue duplicate M600s / re-run a non-idempotent
        pause macro on a printer that is already paused. A failed M400 is
        still logged by _send_gcode."""
        self._send_gcode("M400")  # best-effort planner drain before pausing
        paused = self._send_gcode(self.pause_gcode)
        delivered = bool(paused)
        self._pause_delivered = delivered
        self.state.pause_delivered = delivered
        self._pause_last_attempt_ts = now_s()
        return delivered

    def _trigger_pause(self, reason):
        """Latch and send the pause command due to a detected fault.

        Args:
            reason: Short string describing the fault (e.g. 'jam', 'runout').
        """
        with self._state_lock:
            # Idempotency: if already latched, do nothing (prevents duplicate pause/notify).
            if self.state.latched:
                return
            # A pause only ever makes sense while ARMED. Every caller checks
            # this, but a concurrent reset/disable can land between that check
            # and here — refuse rather than pause a monitor the operator just
            # disabled.
            if self.state.mode != MonitorMode.ARMED:
                return
            # Latch first: even if the send below fails, the fault stays
            # recorded and the main loop keeps retrying delivery.
            self.state.latched = True
            now = now_s()
            self.state.pause_sent_ts = now
            self.state.last_trigger = reason
            self.state.last_trigger_ts = now
            dt = (now - self.state.last_pulse_ts) if self.state.last_pulse_ts else None
            self.logger.emit(
                "pause_triggered",
                reason=reason,
                dt_since_pulse=(round(dt, 3) if dt is not None else None),
                pps=round(self._pps(now), 3),
                pulses_reset=self.state.motion_pulses_since_reset,
                pulses_arm=self.state.motion_pulses_since_arm,
            )
            delivered = self._send_pause_gcode()
            if not delivered:
                self.logger.emit("pause_gcode_failed", reason=reason)

            # Notify (best-effort). Always attempted, even when the serial send
            # failed — the operator must hear about an undelivered pause.
            if reason == "runout":
                message = "📭 Filament runout detected — print paused"
            else:
                message = "🚨 Filament jam detected — print paused (M600)"
            if not delivered:
                message += " (pause G-code send FAILED)"
            self.notifier.send(title="Filament Monitor", message=message, priority=1)

    def _maybe_pause_retry(self):
        """Retry an undelivered pause while latched.

        Called by the main loop each pass. The pause G-code is this daemon's
        whole point, so retries are unlimited: one attempt every
        _pause_retry_interval_s until the write lands (e.g. after a serial
        reconnect) or the operator resets/rearms."""
        with self._state_lock:
            if not self.state.latched or self._pause_delivered:
                return
            now = now_s()
            if now - self._pause_last_attempt_ts < self._pause_retry_interval_s:
                return
            self.logger.emit("pause_retry", reason=self.state.last_trigger)
            self._send_pause_gcode()

    def _maybe_jam(self):
        """Evaluate jam condition based on pulse timing and thresholds.

        This is called periodically by the main loop. Jams can only trigger when explicitly armed and not latched.

        The jam timeout may be adaptive (pps-based) when enabled. Additionally, an optional post-(re)arm grace
        period can be configured to reduce false positives during sparse extrusion (e.g., tiny endgame layers)."""
        with self._state_lock:
            if self.state.mode != MonitorMode.ARMED or self.state.latched:
                return

            now = now_s()
            timeout_s = self._effective_jam_timeout_s(now)

            # Optional post-(re)arm grace gate: do not allow jam latch until the grace criteria are met.
            if (self.arm_grace_pulses > 0 or self.arm_grace_s > 0.0) and self.state.arm_ts:
                elapsed = now - self.state.arm_ts
                # Unset criteria are unsatisfiable; the gate releases on whichever
                # configured criterion is met first.
                pulses_ok = self.arm_grace_pulses > 0 and self.state.motion_pulses_since_arm >= self.arm_grace_pulses
                time_ok = self.arm_grace_s > 0.0 and elapsed >= self.arm_grace_s
                # Pulses-only config: lack of pulses *is* the jam condition, so the
                # gate must still release on time — after the effective timeout —
                # or it would suppress detection indefinitely.
                if self.arm_grace_pulses > 0 and self.arm_grace_s <= 0.0:
                    time_ok = elapsed >= timeout_s
                if not (pulses_ok or time_ok):
                    return

            if now - self.state.last_pulse_ts >= timeout_s:
                self._trigger_pause("jam")

    def _handle_control_marker(self, line):
        """Handle a decoded control marker (serialized under _state_lock).

        Markers are the only control plane for arming/pausing decisions. Jam/runout
        detection is *only* active when explicitly armed via `filmon:arm`.

        Supported markers:
            filmon:reset    - clear latch/counters and DISABLE monitoring
            filmon:enable   - enable monitoring (unarmed)
            filmon:arm      - enable monitoring and ARM jam/runout detection
            filmon:unarm    - keep enabled but disarm detection
            filmon:disable  - disable monitoring
        """
        with self._state_lock:
            low = line.lower()

            # NOTE: reset always wins.
            if CONTROL_RESET in low:
                self.state.mode = MonitorMode.DISABLED
                self.state.latched = False
                self.state.runout_asserted = False
                self.state.motion_pulses_since_reset = 0
                self.state.last_pulse_ts = now_s()
                self.state.motion_pulses_since_arm = 0
                self.state.arm_ts = 0.0
                self._reset_pulse_tracking()
                self.logger.emit("reset")
                return

            # Ignore state transitions while latched except reset (handled above).
            if self.state.latched:
                return

            if CONTROL_DISABLE in low:
                self.state.mode = MonitorMode.DISABLED
                self.logger.emit("disabled")
                return

            if CONTROL_UNARM in low:
                # Idempotent: unarming should not reset counters.
                self.state.mode = MonitorMode.ENABLED
                self._stall_next_idx = 0
                self.logger.emit("unarmed")
                return

            if CONTROL_ARM in low:
                # Start timeout reference at arm time to avoid an immediate jam.
                # Timestamps/counters are written before the mode flip so a
                # concurrent _maybe_jam never sees ARMED with stale timing
                # (defense in depth on top of the state lock).
                now = now_s()
                self.state.motion_pulses_since_arm = 0
                self.state.arm_ts = now
                self.state.last_pulse_ts = now
                self._stall_next_idx = 0
                self.state.mode = MonitorMode.ARMED
                self.logger.emit("armed")
                # A runout that occurred while unarmed must still pause once armed.
                if self._runout_asserted_now():
                    self.state.runout_asserted = True
                    self.logger.emit("runout_asserted")
                    self._trigger_pause("runout")
                return

            if CONTROL_ENABLE in low:
                # Enable only; never arms automatically. Idempotent and does not reset counters.
                if self.state.mode == MonitorMode.ARMED:
                    # Guard: a stray enable (e.g. in a per-layer macro) must not
                    # demote ARMED -> ENABLED and silently turn detection off.
                    self.logger.emit("enabled", ignored="already_armed")
                    return
                if self.state.mode == MonitorMode.ENABLED:
                    self.logger.emit("enabled")
                    return
                self.state.last_pulse_ts = now_s()
                self._stall_next_idx = 0
                self.state.mode = MonitorMode.ENABLED
                self.logger.emit("enabled")
                return

    def start(self):
        """Start GPIO monitoring and the main loop (and serial reader if configured)."""
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        self._loop_thread = t

    def stop(self):
        """Stop threads and clean up GPIO/serial resources."""
        self._stop_evt.set()
        self._control_stop_evt.set()

    def _loop_once(self):
        """One pass of the main loop: drain a serial line, then run periodic checks."""
        try:
            line = self._serial_q.get(timeout=0.2)
            if self.verbose:
                self.logger.emit("serial", line=line)
            self._handle_control_marker(line)
        except queue.Empty:
            pass
        # Prune here unconditionally: the pulse callback is append-only, and
        # while DISABLED neither _maybe_jam nor the heartbeat reaches _pps(),
        # so without this a disabled-but-pulsing daemon grows the deque forever.
        self._prune_pulses(now_s())
        self._reconcile_runout()
        self._maybe_jam()
        self._maybe_pause_retry()
        self._maybe_breadcrumbs()

    def _loop(self):
        """Main periodic loop. Processes control markers and checks for jam/runout faults.

        Any unexpected exception is fatal by design: emit 'monitor_loop_error',
        set the stop event so the supervisor exits non-zero, and let systemd
        restart a daemon whose core loop can no longer be trusted."""
        while not self._stop_evt.is_set():
            try:
                self._loop_once()
            except Exception as e:
                try:
                    self.logger.emit("monitor_loop_error", error=str(e))
                except Exception:
                    pass
                self._stop_evt.set()
                break


