import json
import socket
import time
import pytest


class CapturingLogger:
    def __init__(self):
        self.events = []
    def emit(self, event: str, **fields):
        self.events.append((event, fields))


class DummyDigitalInputDevice:
    """GPIO stub capturing constructor args and callbacks."""
    def __init__(self, pin, pull_up=True, **kwargs):
        self.pin = pin
        self.pull_up = pull_up
        self.kwargs = kwargs
        self.when_activated = None
        self.when_deactivated = None


class DummySerial:
    def __init__(self):
        self.writes = []
    def write(self, data: bytes):
        self.writes.append(data.decode(errors="replace"))
    def flush(self):
        pass


def _make_monitor(monkeypatch, *, rearm_button_gpio=None):
    m = load_module()
    monkeypatch.setattr(m, "DigitalInputDevice", DummyDigitalInputDevice, raising=True)

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


def test_control_socket_rearm_clears_latch_and_arms(monkeypatch, tmp_path):
    m, mon, logger = _make_monitor(monkeypatch)

    # Put monitor into a "latched" state to simulate a jam pause.
    mon.state.enabled = True
    mon.state.armed = True
    mon.state.latched = True
    mon.state.motion_pulses_since_reset = 123
    mon.state.motion_pulses_since_arm = 45

    sock_path = tmp_path / "filmon.sock"
    mon.start_control_socket(str(sock_path))

    # Wait briefly for server thread to bind.
    deadline = time.time() + 2.0
    while time.time() < deadline and not sock_path.exists():
        time.sleep(0.01)

    resp = _send_cmd(str(sock_path), "rearm")
    assert resp.get("ok") is True

    assert mon.state.latched is False
    assert mon.state.enabled is True
    assert mon.state.armed is True
    assert mon.state.motion_pulses_since_reset == 0
    assert mon.state.motion_pulses_since_arm == 0

    mon.stop()


def test_rearm_button_is_active_low_with_pullup(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, rearm_button_gpio=25)
    assert mon.rearm_button is not None
    assert mon.rearm_button.pin == 25
    # Active-low only: internal pull-up enabled and action on deactivation (high->low)
    assert mon.rearm_button.pull_up is True
    assert mon.rearm_button.when_deactivated is not None
    assert mon.rearm_button.when_activated is None


def test_rearm_button_debounce(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, rearm_button_gpio=25)

    calls = {"n": 0}
    def fake_rearm():
        calls["n"] += 1
    mon._cmd_rearm = fake_rearm  # type: ignore

    t = {"now": 100.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    # First press triggers.
    mon._on_rearm_button()
    assert calls["n"] == 1

    # Within debounce interval -> ignored.
    t["now"] += 0.10
    mon._on_rearm_button()
    assert calls["n"] == 1

    # After debounce interval -> triggers.
    t["now"] += 0.30
    mon._on_rearm_button()
    assert calls["n"] == 2
