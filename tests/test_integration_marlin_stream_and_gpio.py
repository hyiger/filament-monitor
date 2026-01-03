import re
import math
from pathlib import Path
from typing import List

import pytest


class FakeSerial:
    """Minimal serial-like object capturing writes from the monitor."""
    def __init__(self):
        self.writes: List[bytes] = []

    def write(self, b: bytes):
        self.writes.append(b)
        return len(b)

    def flush(self):
        # pyserial compatibility (no-op for test double)
        return None


def _extract_serial_payloads(log_text: str) -> List[str]:
    payloads: List[str] = []
    for ln in log_text.splitlines():
        if "serial line=" not in ln:
            continue
        payload = ln.split("serial line=", 1)[1].strip()
        if payload:
            payloads.append(payload)
    return payloads


def _sum_positive_extrusion_mm(gcode_text: str) -> float:
    """Sum positive extrusion length from a G-code snippet (absolute or relative E)."""
    e_abs = True
    last_e = 0.0
    total = 0.0
    for raw in gcode_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("M82"):
            e_abs = True
            continue
        if line.startswith("M83"):
            e_abs = False
            continue
        if not (line.startswith("G0") or line.startswith("G1")):
            continue
        m = re.search(r"\bE(-?\d*\.?\d+)\b", line)
        if not m:
            continue
        e = float(m.group(1))
        de = (e - last_e) if e_abs else e
        if e_abs:
            last_e = e
        if de > 0:
            total += de
    return total


# Local minimal test helpers to avoid importing from tests as a package
class CapturingLogger:
    def __init__(self):
        self.events = []

    def emit(self, name: str, **kwargs):
        self.events.append((name, kwargs))


class DummyDigitalInputDevice:
    def __init__(self, pin, pull_up=True):
        self.pin = pin
        self.pull_up = pull_up
        self.when_activated = None
        self.when_deactivated = None

    def close(self):
        pass


@pytest.mark.integration
def test_marlin_like_serial_stream_gpio_activity_rearm_then_runout(monkeypatch):
    """Log-aligned integration test (in-process).

    Sequence:
      - reset/enable/arm via comment-style markers (as seen in monitor.log)
      - pulses => no jam
      - stop pulses => jam => M400 then M600, latched
      - pulses while latched => no extra pause
      - long-press rearm button => clears latch + arms
      - pulses => ok
      - runout asserted => M400 then M600, latched
    """
    m = load_module()  # from tests/conftest.py

    monkeypatch.setattr(m, "DigitalInputDevice", DummyDigitalInputDevice, raising=True)

    repo_root = Path(__file__).resolve().parents[1]
    log_text = (repo_root / "tests" / "data" / "monitor.log").read_text(errors="replace")
    gcode_text = (repo_root / "tests" / "data" / "sample.gcode").read_text(errors="replace")

    serial_payloads = _extract_serial_payloads(log_text)
    assert "// filmon:reset" in serial_payloads
    assert "// filmon:enable" in serial_payloads
    assert "// filmon:arm" in serial_payloads

    logger = CapturingLogger()
    state = m.MonitorState()

    mon = m.FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=26,
        runout_gpio=27,
        runout_active_high=False,
        runout_debounce_s=0.02,
        jam_timeout_s=0.8,
        arm_min_pulses=3,
        pause_gcode="M600",
        verbose=True,
        breadcrumb_interval_s=0.2,
        pulse_window_s=0.5,
        stall_thresholds_s="0.4,0.6",
        rearm_button_gpio=25,
        rearm_button_active_high=False,
        rearm_button_debounce_s=0.05,
        rearm_button_long_press_s=0.5,
    )

    fake_ser = FakeSerial()
    mon.attach_serial(fake_ser)

    t = {"now": 0.0}
    monkeypatch.setattr(m, "now_s", lambda: t["now"], raising=True)

    for line in [
        "start",
        "echo:busy: processing",
        "T:250.0 /250.0 B:100.0 /100.0 @:0 B@:0",
        "X:186.00 Y:-2.50 Z:15.00 E:0.00 Count X: 0 Y:0 Z:0",
        "ok",
        "// filmon:reset",
        "ok",
        "// filmon:enable",
        "ok",
        "// filmon:arm",
        "ok",
    ]:
        mon._handle_control_marker(line)

    assert mon.state.enabled is True
    assert mon.state.armed is True
    assert mon.state.latched is False

    mm_per_pulse = 2.88
    total_e = min(_sum_positive_extrusion_mm(gcode_text), 20.0)
    pulses = max(6, int(math.ceil(total_e / mm_per_pulse)))
    dt = 1.0 / pulses

    # activity => no jam
    for _ in range(pulses):
        mon._on_motion_pulse()
        t["now"] += dt
        mon._maybe_jam()

    assert b"M600" not in b"".join(fake_ser.writes)

    # jam: stop pulses past timeout
    t["now"] += 1.2
    mon._maybe_jam()

    writes = b"".join(fake_ser.writes)
    assert writes.count(b"M600") == 1
    assert b"M400" in writes
    assert mon.state.latched is True

    # resumed activity while latched => no extra pause
    for _ in range(5):
        mon._on_motion_pulse()
        t["now"] += 0.05
        mon._maybe_jam()
    assert b"".join(fake_ser.writes).count(b"M600") == 1

    # long-press rearm
    mon._on_rearm_button_press()
    t["now"] += 0.6
    mon._on_rearm_button_release()
    assert mon.state.latched is False
    assert mon.state.enabled is True
    assert mon.state.armed is True

    # more activity
    for _ in range(8):
        mon._on_motion_pulse()
        t["now"] += 0.05
        mon._maybe_jam()
    assert b"".join(fake_ser.writes).count(b"M600") == 1

    # runout asserted
    t["now"] += 0.1
    mon._on_runout_asserted()
    writes = b"".join(fake_ser.writes)
    assert writes.count(b"M600") == 2
    assert b"M400" in writes
    assert mon.state.latched is True

    mon.stop()
