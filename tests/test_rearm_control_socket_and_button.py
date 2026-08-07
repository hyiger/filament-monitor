import json
import shutil
import socket
import tempfile
import time
import pytest

from builtins import CapturingLogger, DummyGPIO, DummySerial
from filmon.state import MonitorMode


class DummyDigitalInputDevice:
    """GPIO stub capturing constructor args and callbacks."""
    def __init__(self, pin, pull_up=True, **kwargs):
        self.pin = pin
        self.pull_up = pull_up
        self.kwargs = kwargs
        self.when_activated = None
        self.when_deactivated = None


def _make_monitor(monkeypatch, *, rearm_button_gpio=None):
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
        # active-low only build: argument exists but defaults to False
        rearm_button_active_high=False,
        rearm_button_debounce_s=0.25,
        rearm_button_long_press_s=1.5,
        gpio_factory=DummyGPIO,
    )
    mon._ser = DummySerial()
    return m, mon, logger


def _send_cmd(sock_path: str, cmd: str) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(sock_path)
    s.sendall((cmd.strip() + "\n").encode())
    data = b""
    while b"\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    line = data.split(b"\n", 1)[0].decode(errors="replace").strip()
    return json.loads(line) if line else {}


def test_control_socket_rearm_clears_latch_and_arms(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch)

    # Put monitor into a "latched" state to simulate a jam pause.
    mon.state.mode = MonitorMode.ARMED
    mon.state.latched = True
    mon.state.motion_pulses_since_reset = 123
    mon.state.motion_pulses_since_arm = 45

    # Use /tmp directly to avoid macOS's 104-byte AF_UNIX path length limit.
    # (pytest's tmp_path can exceed it on macOS, so clean up manually instead.)
    tmpdir = tempfile.mkdtemp(dir="/tmp")
    try:
        sock_path = tmpdir + "/filmon.sock"
        mon.start_control_socket(sock_path)

        # Wait briefly for server thread to bind.
        import os
        deadline = time.time() + 2.0
        while time.time() < deadline and not os.path.exists(sock_path):
            time.sleep(0.01)

        resp = _send_cmd(sock_path, "rearm")
        assert resp.get("ok") is True

        assert mon.state.latched is False
        assert mon.state.mode == MonitorMode.ARMED
        assert mon.state.motion_pulses_since_reset == 0
        assert mon.state.motion_pulses_since_arm == 0

        mon.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rearm_button_is_active_low_with_pullup(monkeypatch):
    m = load_module()
    monkeypatch.setattr(m.monitor, "DigitalInputDevice", DummyDigitalInputDevice, raising=True)

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
        rearm_button_gpio=25,
        rearm_button_active_high=False,   # active-low
        rearm_button_debounce_s=0.25,
        rearm_button_long_press_s=1.5,
    )

    assert mon.rearm_button is not None
    assert mon.rearm_button.pin == 25
    assert mon.rearm_button.pull_up is True
    # With pull_up=True, gpiozero's "active" means pressed (pin low), so the
    # mapping is press=when_activated, release=when_deactivated even active-low.
    assert mon.rearm_button.when_activated == mon._on_rearm_button_press
    assert mon.rearm_button.when_deactivated == mon._on_rearm_button_release

    mon.stop()


def test_rearm_button_short_press_triggers_reset(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, rearm_button_gpio=25)

    # Patch time source
    tnow = {"t": 100.0}
    monkeypatch.setattr(m.monitor, "now_s", lambda: tnow["t"], raising=True)

    # Start from a latched + enabled/armed state
    mon.state.mode = MonitorMode.ARMED
    mon.state.latched = True
    mon.state.motion_pulses_since_reset = 10
    mon.state.motion_pulses_since_arm = 5

    # Short press: press then release before long-press threshold
    mon._on_rearm_button_press()
    tnow["t"] += 0.4
    mon._on_rearm_button_release()

    # Reset semantics: disabled + unlatched, counters cleared
    assert mon.state.mode == MonitorMode.DISABLED
    assert mon.state.latched is False
    assert mon.state.motion_pulses_since_reset == 0
    assert mon.state.motion_pulses_since_arm == 0

    mon.stop()


def test_rearm_button_long_press_triggers_rearm(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, rearm_button_gpio=25)

    # Patch time source
    tnow = {"t": 200.0}
    monkeypatch.setattr(m.monitor, "now_s", lambda: tnow["t"], raising=True)

    # Start latched
    mon.state.mode = MonitorMode.ARMED
    mon.state.latched = True
    mon.state.motion_pulses_since_reset = 10
    mon.state.motion_pulses_since_arm = 5

    mon._on_rearm_button_press()
    tnow["t"] += 2.0   # >= 1.5s long-press
    mon._on_rearm_button_release()

    assert mon.state.latched is False
    assert mon.state.mode == MonitorMode.ARMED
    assert mon.state.motion_pulses_since_reset == 0
    assert mon.state.motion_pulses_since_arm == 0

    mon.stop()


def test_rearm_button_debounce_applies_on_press_edge(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, rearm_button_gpio=25)

    tnow = {"t": 300.0}
    monkeypatch.setattr(m.monitor, "now_s", lambda: tnow["t"], raising=True)

    # Spy on actions
    calls = {"reset": 0, "rearm": 0}
    monkeypatch.setattr(mon, "_cmd_rearm", lambda: calls.__setitem__("rearm", calls["rearm"] + 1), raising=True)

    def fake_handle(line):
        if m.CONTROL_RESET.lower() in line.lower():
            calls["reset"] += 1
    monkeypatch.setattr(mon, "_handle_control_marker", fake_handle, raising=True)

    # First press/release -> short press reset
    mon._on_rearm_button_press()
    tnow["t"] += 0.05
    mon._on_rearm_button_release()
    assert calls["reset"] == 1
    assert calls["rearm"] == 0

    # Immediate second press within debounce -> ignored; release should do nothing
    tnow["t"] += 0.10
    mon._on_rearm_button_press()
    tnow["t"] += 0.05
    mon._on_rearm_button_release()
    assert calls["reset"] == 1
    assert calls["rearm"] == 0

    # After debounce -> works again
    tnow["t"] += 0.3
    mon._on_rearm_button_press()
    tnow["t"] += 0.2
    mon._on_rearm_button_release()
    assert calls["reset"] == 2
    assert calls["rearm"] == 0

    mon.stop()

