import pytest


class CapturingLogger:
    """Minimal logger that matches the monitor's .emit(event, **fields) contract."""
    def __init__(self):
        self.events = []

    def emit(self, event: str, **fields):
        self.events.append((event, fields))


class DummyDigitalInputDevice:
    """GPIO stub to avoid hardware access in unit tests."""
    def __init__(self, *args, **kwargs):
        self.when_activated = None
        self.when_deactivated = None


class DummySerial:
    def __init__(self):
        self.writes = []

    def write(self, data: bytes):
        self.writes.append(data.decode(errors="replace"))

    def flush(self):
        pass


def _make_monitor(monkeypatch, jam_timeout_s=1.0, **kwargs):
    m = load_module()
    # Patch the GPIO DigitalInputDevice used by the monitor.
    monkeypatch.setattr(m.monitor, "DigitalInputDevice", DummyDigitalInputDevice, raising=True)

    logger = CapturingLogger()
    state = m.MonitorState()
    mon = m.FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=26,
        runout_gpio=27,
        runout_active_high=False,
        runout_debounce_s=0.0,
        jam_timeout_s=jam_timeout_s,
        arm_min_pulses=12,  # ignored in marker-only arming model
        pause_gcode="M600",
        verbose=False,
        **kwargs,
    )
    mon.attach_serial(DummySerial())
    return m, mon, logger


def test_enable_without_arm_never_jams(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 100.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("M118 A1 filmon:enable")
    assert mon.state.enabled is True
    assert mon.state.armed is False

    # Advance beyond timeout; because we're unarmed, jam must not trigger.
    t["now"] += 5.0
    mon._maybe_jam()
    assert mon.state.latched is False
    assert mon._ser.writes == []


def test_arm_enables_jam_detection_and_latches(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 200.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("M118 A1 filmon:enable")
    mon._handle_control_marker("M118 A1 filmon:arm")
    assert mon.state.enabled is True
    assert mon.state.armed is True

    # No pulses arrive; advance beyond timeout → jam triggers once and latches.
    t["now"] += 2.0
    mon._maybe_jam()
    assert mon.state.latched is True
    assert any("M600" in w for w in mon._ser.writes)


def test_latch_blocks_retrigger_until_reset(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 300.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")
    t["now"] += 2.0
    mon._maybe_jam()
    assert mon.state.latched is True
    writes1 = list(mon._ser.writes)

    # Even if time advances, no additional pauses should be issued while latched.
    t["now"] += 10.0
    mon._maybe_jam()
    assert mon._ser.writes == writes1

    # Reset clears latch and disables. Re-arm allows a second trigger.
    mon._handle_control_marker("filmon:reset")
    assert mon.state.latched is False
    assert mon.state.enabled is False
    assert mon.state.armed is False

    mon._handle_control_marker("filmon:arm")
    t["now"] += 2.0
    mon._maybe_jam()
    # Each trigger sends M400 then pause_gcode (2 writes)
    assert len(mon._ser.writes) == len(writes1) + 2


def test_runout_requires_arm(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, jam_timeout_s=5.0)
    t = {"now": 400.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:enable")
    assert mon.state.armed is False

    # Runout asserted while unarmed: should not pause.
    mon._on_runout_asserted()
    assert mon.state.runout_asserted is True
    assert mon._ser.writes == []

    # Arm then assert runout again: should pause and latch.
    mon._handle_control_marker("filmon:arm")
    mon._on_runout_asserted()
    assert mon.state.latched is True
    assert any("M600" in w for w in mon._ser.writes)


def test_stop_ignores_late_motion_callbacks(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, jam_timeout_s=5.0)
    before_total = mon.state.motion_pulses_total
    before_since = mon.state.motion_pulses_since_reset
    before_ts = mon.state.last_pulse_ts

    mon.stop()
    mon._on_motion_pulse()

    assert mon.state.motion_pulses_total == before_total
    assert mon.state.motion_pulses_since_reset == before_since
    assert mon.state.last_pulse_ts == before_ts


def test_post_arm_grace_gate_blocks_false_jam(monkeypatch):
    """If configured, jam latching is suppressed right after (re)arm until pulses/time criteria are met."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_s=1.0,
        arm_grace_pulses=12,
        arm_grace_s=12.0,
    )
    t = {"now": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")
    assert mon.state.armed is True

    # Advance beyond the base timeout, but still within grace window and with 0 pulses since arm.
    t["now"] += 2.0
    mon._maybe_jam()
    assert mon.state.latched is False
    assert mon._ser.writes == []

    # Once the grace time elapses, jam detection can trigger.
    t["now"] += 12.0
    mon._maybe_jam()
    assert mon.state.latched is True
    assert any("M600" in w for w in mon._ser.writes)


def test_adaptive_timeout_scales_with_pps(monkeypatch):
    """Adaptive jam timeout should scale with recent pps and clamp when pps collapses."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_s=8.0,
        jam_timeout_adaptive=True,
        jam_timeout_min_s=6.0,
        jam_timeout_max_s=18.0,
        jam_timeout_k=16.0,
        jam_timeout_pps_floor=0.3,
        jam_timeout_ema_halflife_s=0.0,  # make EMA track instantaneous pps for deterministic test
        pulse_window_s=2.0,
    )
    t = {"now": 2000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")

    # Simulate pulses at ~2 pps over the 2s window => expected effective timeout ~ 16/2 = 8s.
    for _ in range(4):
        mon._on_motion_pulse()
        t["now"] += 0.5

    eff = mon._effective_jam_timeout_s(t["now"])
    assert 7.0 <= eff <= 9.0

    # After the window expires (pps->0), the effective timeout should clamp to jam_timeout_max_s.
    t["now"] += 5.0
    eff2 = mon._effective_jam_timeout_s(t["now"])
    assert eff2 == pytest.approx(18.0, abs=0.01)

    # With no pulses, jam should only trigger after the clamped max timeout.
    t["now"] = mon.state.last_pulse_ts + 17.9
    mon._maybe_jam()
    assert mon.state.latched is False

    t["now"] = mon.state.last_pulse_ts + 18.1
    mon._maybe_jam()
    assert mon.state.latched is True
