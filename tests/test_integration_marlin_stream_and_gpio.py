import re
import math
from pathlib import Path
from typing import List, Tuple

import pytest


class FakeSerial:
    """Minimal serial-like object capturing writes from the monitor."""
    def __init__(self):
        self.writes: List[bytes] = []

    def write(self, b: bytes):
        self.writes.append(b)
        return len(b)


def _extract_serial_payloads(log_text: str) -> List[str]:
    payloads = []
    for ln in log_text.splitlines():
        if "serial line=" not in ln:
            continue
        payload = ln.split("serial line=", 1)[1].strip()
        if payload:
            payloads.append(payload)
    return payloads


def _sum_positive_extrusion_mm(gcode_text: str) -> float:
    """Sum positive E deltas from G0/G1 in a G-code snippet.

    Supports both absolute (M82) and relative (M83) extrusion modes.
    """
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


@pytest.mark.integration
def test_marlin_like_serial_stream_gpio_activity_rearm_then_runout(monkeypatch):
    """Integration-style scenario aligned to monitor.log:

    1) Feed comment-style markers: `// filmon:reset`, `// filmon:enable`, `// filmon:arm`
    2) Simulate normal printing activity via motion GPIO pulses (no jam)
    3) Simulate a filament jam by stopping pulses past `jam_timeout_s` => expect `M400` then `M600`, and latch
    4) Simulate "resume" activity (pulses continue) while latched => should NOT trigger additional pauses
    5) Long-press the optional rearm button => clears latch and arms
    6) Simulate more activity (pulses) => no jam
    7) Simulate filament runout => expect another `M400` then `M600`, and latch again
    """
    m = load_module()  # provided by tests/conftest.py

    from tests.test_rearm_control_socket_and_button import DummyDigitalInputDevice, CapturingLogger
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

    # Deterministic time
    t = {"now": 0.0}
    monkeypatch.setattr(m, "now_s", lambda: t["now"], raising=True)

    # Feed chatter + markers (matching monitor.log style)
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

    # --- Phase A: normal activity (pulses) ---
    mm_per_pulse = 2.88
    total_e = _sum_positive_extrusion_mm(gcode_text)
    total_e = min(total_e, 20.0)
    pulses = max(6, int(math.ceil(total_e / mm_per_pulse)))
    dt = 1.0 / pulses

    for _ in range(pulses):
        mon._on_motion_pulse()
        t["now"] += dt
        mon._maybe_jam()

    writes = b"".join(fake_ser.writes)
    assert writes.count(b"M600") == 0

    # --- Phase B: jam (stop pulses past timeout) ---
    t["now"] += 1.2
    mon._maybe_jam()

    writes = b"".join(fake_ser.writes)
    assert b"M400" in writes
    assert writes.count(b"M600") == 1
    assert mon.state.latched is True

    # --- Phase C: "resumed" activity while latched (should not repause) ---
    for _ in range(5):
        mon._on_motion_pulse()
        t["now"] += 0.05
        mon._maybe_jam()

    writes = b"".join(fake_ser.writes)
    assert writes.count(b"M600") == 1, "No additional pause should occur while latched."

    # --- Phase D: long press rearm button ---
    mon._on_rearm_button_press()
    t["now"] += 0.6  # >= long_press threshold (0.5s)
    mon._on_rearm_button_release()

    assert mon.state.latched is False
    assert mon.state.enabled is True
    assert mon.state.armed is True

    # --- Phase E: more activity after rearm ---
    for _ in range(8):
        mon._on_motion_pulse()
        t["now"] += 0.05
        mon._maybe_jam()

    writes = b"".join(fake_ser.writes)
    assert writes.count(b"M600") == 1

    # --- Phase F: filament runout ---
    # Ensure we're past debounce window
    t["now"] += 0.1
    mon._on_runout_asserted()

    writes = b"".join(fake_ser.writes)
    assert writes.count(b"M600") == 2
    assert mon.state.latched is True

    mon.stop()

