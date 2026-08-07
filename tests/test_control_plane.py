import importlib.util
import json
import os
import socket
import tempfile
import time
from pathlib import Path

from builtins import DummyGPIO
from filmon.state import MonitorMode


class CapturingLogger:
    def __init__(self):
        self.events = []
    def emit(self, event: str, **fields):
        self.events.append((event, fields))


class DummySerial:
    def __init__(self):
        self.writes = []
    def write(self, data: bytes):
        self.writes.append(data.decode(errors="replace"))
    def flush(self):
        pass


def _make_monitor(monkeypatch, *, rearm_button_gpio=None, jam_timeout_adaptive=False):
    m = load_module()

    logger = CapturingLogger()
    state = m.MonitorState()
    mon = m.FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=26,
        runout_gpio=27,
        runout_active_high=True,
        runout_debounce_s=0.02,
        jam_timeout_s=1.0,
        arm_min_pulses=1,
        pause_gcode="M600",
        breadcrumb_interval_s=0.5,
        pulse_window_s=1.0,
        stall_thresholds_s="0.5,0.8",
        rearm_button_gpio=rearm_button_gpio,
        rearm_button_active_high=False,
        rearm_button_debounce_s=0.25,
        rearm_button_long_press_s=1.5,
        jam_timeout_adaptive=jam_timeout_adaptive,
        gpio_factory=DummyGPIO,
    )
    mon._ser = DummySerial()
    return m, mon, logger


def _load_filmonctl():
    script = Path(__file__).resolve().parents[1] / "filmonctl.py"
    spec = importlib.util.spec_from_file_location("filmonctl", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _start_socket(mon):
    """Start the control socket on a fresh /tmp path and wait for the bind."""
    # Use /tmp directly to avoid macOS's 104-byte AF_UNIX path length limit.
    tmpdir = tempfile.mkdtemp(dir="/tmp")
    sock_path = tmpdir + "/filmon.sock"
    mon.start_control_socket(sock_path)
    deadline = time.time() + 2.0
    while time.time() < deadline and not os.path.exists(sock_path):
        time.sleep(0.01)
    return sock_path


def _send_raw(sock_path: str, payload: bytes) -> dict:
    """Send raw bytes to the control socket and return the parsed JSON reply."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(sock_path)
    s.sendall(payload)
    data = b""
    while b"\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    line = data.split(b"\n", 1)[0].decode(errors="replace").strip()
    return json.loads(line) if line else {}


# ---------------- _handle_control_command ----------------

def test_status_response_is_json_serializable_with_mode_and_adaptive(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, jam_timeout_adaptive=True)

    resp = mon._handle_control_command("status")
    assert resp["ok"] is True

    # asdict()-based state must serialize without a custom encoder.
    encoded = json.dumps(resp, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["state"]["mode"] == "disabled"
    assert decoded["state"]["jam_timeout_adaptive"] is True
    assert "latched" in decoded["state"]

    # The removed boolean fields must not resurface.
    assert "enabled" not in decoded["state"]
    assert "armed" not in decoded["state"]


def test_unknown_and_empty_commands_are_rejected(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch)

    resp = mon._handle_control_command("bogus")
    assert resp["ok"] is False
    assert "unknown command" in resp["error"]

    resp = mon._handle_control_command("")
    assert resp["ok"] is False
    assert resp["error"] == "empty command"


def test_rearm_command_when_latched_clears_latch_and_arms(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch)

    mon.state.mode = MonitorMode.ARMED
    mon.state.latched = True
    mon.state.motion_pulses_since_reset = 123
    mon.state.motion_pulses_since_arm = 45

    resp = mon._handle_control_command("rearm")
    assert resp == {"ok": True}
    assert mon.state.latched is False
    assert mon.state.mode == MonitorMode.ARMED
    assert mon.state.motion_pulses_since_reset == 0
    assert mon.state.motion_pulses_since_arm == 0
    assert ("rearmed", {}) in logger.events


def test_rearm_command_when_not_latched_is_refused_and_state_unchanged(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch)

    mon.state.mode = MonitorMode.ENABLED
    mon.state.latched = False
    mon.state.motion_pulses_since_reset = 7
    mon.state.motion_pulses_since_arm = 3
    mon.state.arm_ts = 111.0

    resp = mon._handle_control_command("rearm")
    assert resp == {"ok": False, "error": "not latched"}

    # State must be untouched: no arming an idle printer.
    assert mon.state.mode == MonitorMode.ENABLED
    assert mon.state.latched is False
    assert mon.state.motion_pulses_since_reset == 7
    assert mon.state.motion_pulses_since_arm == 3
    assert mon.state.arm_ts == 111.0

    events = [e for e, _ in logger.events]
    assert "rearm_ignored" in events
    assert "rearmed" not in events


def test_test_notify_command_disabled_notifier_reports_error(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch)
    # Default test env has no FILMON_NOTIFY/PUSHOVER_*, so the notifier is off.
    assert mon.notifier.enabled is False

    resp = mon._handle_control_command("test-notify")
    assert resp["ok"] is False
    assert resp["enabled"] is False
    assert "notifier disabled" in resp["error"]


def test_test_notify_command_sends_via_daemon_notifier(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch)

    calls = []
    class FakeNotifier:
        enabled = True
        def send_sync(self, title, message, priority=0):
            calls.append((title, message, priority))
            return True
    mon.notifier = FakeNotifier()

    resp = mon._handle_control_command("test-notify")
    assert resp == {"ok": True, "enabled": True}
    assert calls == [("Filament Monitor", "Test notification (via daemon)", 0)]


def test_test_notify_command_reports_delivery_failure(monkeypatch):
    """The reply must reflect the real HTTP outcome — a fire-and-forget send
    would report success exactly when credentials/network are broken (Codex
    review on #40)."""
    m, mon, logger = _make_monitor(monkeypatch)

    class FakeNotifier:
        enabled = True
        def send_sync(self, title, message, priority=0):
            return False  # e.g. HTTP 400 from a rotated token
    mon.notifier = FakeNotifier()

    resp = mon._handle_control_command("test-notify")
    assert resp["ok"] is False
    assert resp["enabled"] is True
    assert "failed" in resp["error"]


# ---------------- Real AF_UNIX socket round-trips ----------------

def test_control_socket_status_round_trip(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch)
    sock_path = _start_socket(mon)

    resp = _send_raw(sock_path, b"status\n")
    assert resp["ok"] is True
    assert resp["state"]["mode"] == "disabled"
    assert "jam_timeout_adaptive" in resp["state"]

    mon.stop()


def test_control_socket_splits_command_at_first_newline(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch)
    mon.state.mode = MonitorMode.ARMED
    mon.state.latched = True
    sock_path = _start_socket(mon)

    # Pipelined input: only the first line is the command.
    resp = _send_raw(sock_path, b"rearm\nstatus\n")
    assert resp == {"ok": True}          # rearm response, not "unknown command"
    assert "state" not in resp           # the trailing "status" was ignored
    assert mon.state.latched is False
    assert mon.state.mode == MonitorMode.ARMED

    mon.stop()


# ---------------- filmonctl client robustness ----------------

def test_filmonctl_send_returns_clean_error_when_daemon_unreachable():
    ctl = _load_filmonctl()
    missing = tempfile.mkdtemp(dir="/tmp") + "/no-such-daemon.sock"

    resp = ctl._send(missing, "status")
    assert resp["ok"] is False
    assert f"cannot reach daemon at {missing}" in resp["error"]


# ---------------- Notifier delivery logging and retries ----------------

class FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


def test_notifier_success_emits_notify_sent(monkeypatch):
    import filmon.notify as notify_mod

    logger = CapturingLogger()
    monkeypatch.setattr(notify_mod.requests, "post", lambda *a, **k: FakeResp(200, {"status": 1}))

    n = notify_mod.Notifier(enabled=True, pushover_token="t", pushover_user="u", logger=logger)
    n._send_sync("t", "m", 0)

    assert [e for e, _ in logger.events] == ["notify_sent"]
    _, fields = logger.events[0]
    assert fields["priority"] == 0
    assert fields["attempt"] == 1


def test_notifier_http_400_emits_notify_failed(monkeypatch):
    import filmon.notify as notify_mod

    logger = CapturingLogger()
    monkeypatch.setattr(notify_mod.requests, "post", lambda *a, **k: FakeResp(400, {"status": 0, "errors": ["application token is invalid"]}))

    n = notify_mod.Notifier(enabled=True, pushover_token="t", pushover_user="u", logger=logger)
    n._send_sync("t", "m", 0)   # priority 0: single attempt, no retries

    assert [e for e, _ in logger.events] == ["notify_failed"]
    _, fields = logger.events[0]
    assert fields["status"] == 400


def test_notifier_exception_emits_notify_failed(monkeypatch):
    import filmon.notify as notify_mod

    logger = CapturingLogger()
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(notify_mod.requests, "post", boom)

    n = notify_mod.Notifier(enabled=True, pushover_token="t", pushover_user="u", logger=logger)
    n._send_sync("t", "m", 0)

    assert [e for e, _ in logger.events] == ["notify_failed"]
    _, fields = logger.events[0]
    assert "network down" in fields["error"]


def test_notifier_priority_1_retries_with_backoff(monkeypatch):
    import filmon.notify as notify_mod

    logger = CapturingLogger()
    attempts = {"n": 0}
    def failing_post(*a, **k):
        attempts["n"] += 1
        return FakeResp(500, None)
    monkeypatch.setattr(notify_mod.requests, "post", failing_post)

    sleeps = []
    monkeypatch.setattr(notify_mod.time, "sleep", lambda s: sleeps.append(s))

    n = notify_mod.Notifier(enabled=True, pushover_token="t", pushover_user="u", logger=logger)
    n._send_sync("t", "m", 1)

    # 1 initial + 2 retries, with backoff between attempts.
    assert attempts["n"] == 3
    assert sleeps == [n.RETRY_BACKOFF_S, n.RETRY_BACKOFF_S]
    assert [e for e, _ in logger.events] == ["notify_failed"] * 3
    assert [f["attempt"] for _, f in logger.events] == [1, 2, 3]


def test_notifier_priority_1_stops_retrying_after_success(monkeypatch):
    import filmon.notify as notify_mod

    logger = CapturingLogger()
    responses = [FakeResp(500, None), FakeResp(200, {"status": 1})]
    monkeypatch.setattr(notify_mod.requests, "post", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(notify_mod.time, "sleep", lambda s: None)

    n = notify_mod.Notifier(enabled=True, pushover_token="t", pushover_user="u", logger=logger)
    n._send_sync("t", "m", 1)

    assert [e for e, _ in logger.events] == ["notify_failed", "notify_sent"]
    assert responses == []


# ---------------- Rearm button gating ----------------

def test_rearm_button_long_press_when_not_latched_logs_ignored_and_does_not_arm(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, rearm_button_gpio=25)

    tnow = {"t": 400.0}
    monkeypatch.setattr(m.monitor, "now_s", lambda: tnow["t"], raising=True)

    mon.state.mode = MonitorMode.ENABLED
    mon.state.latched = False

    mon._on_rearm_button_press()
    tnow["t"] += 2.0   # >= 1.5s long-press
    mon._on_rearm_button_release()

    assert mon.state.mode == MonitorMode.ENABLED
    assert mon.state.latched is False

    events = [e for e, _ in logger.events]
    assert "rearm_ignored" in events
    assert "rearmed" not in events
    _, fields = next(ev for ev in logger.events if ev[0] == "rearm_ignored")
    assert fields["reason"] == "not latched"

    mon.stop()


def test_slow_test_notify_does_not_block_other_commands(monkeypatch):
    """test-notify blocks until the HTTP outcome; that must not starve
    state-changing commands behind it on the single accept loop (Codex
    review on #40)."""
    import threading as _threading

    m, mon, logger = _make_monitor(monkeypatch)

    release = _threading.Event()
    class SlowNotifier:
        enabled = True
        def send_sync(self, title, message, priority=0):
            release.wait(10.0)  # simulates a slow Pushover round-trip
            return True
    mon.notifier = SlowNotifier()

    sock_path = _start_socket(mon)
    try:
        results = {}
        def call(cmd, key):
            results[key] = _send_raw(sock_path, (cmd + "\n").encode())

        t_notify = _threading.Thread(target=call, args=("test-notify", "notify"), daemon=True)
        t_notify.start()
        time.sleep(0.2)  # let the notify command occupy its handler thread

        # A status command must complete while test-notify is still blocked.
        t_status = _threading.Thread(target=call, args=("status", "status"), daemon=True)
        t_status.start()
        t_status.join(timeout=2.0)
        assert not t_status.is_alive(), "status starved behind test-notify"
        assert results["status"]["ok"] is True

        release.set()
        t_notify.join(timeout=2.0)
        assert not t_notify.is_alive()
        assert results["notify"]["ok"] is True
    finally:
        release.set()
        mon.stop()
