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
        self.when_deactivated = None
        self.when_activated = None


class DummySerial:
    def __init__(self):
        self.writes = []

    def write(self, b: bytes):
        self.writes.append(b)

    def flush(self):
        pass


def _make_monitor(monkeypatch, *, arm_min_pulses=12, jam_timeout_s=2.0):
    m = load_module()
    # Avoid touching real GPIO backends in unit tests.
    monkeypatch.setattr(m, "DigitalInputDevice", DummyDigitalInputDevice, raising=True)

    state = m.MonitorState()
    logger = CapturingLogger()
    mon = m.FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=26,
        runout_gpio=None,
        runout_active_high=False,
        runout_debounce_s=0.0,
        jam_timeout_s=jam_timeout_s,
        arm_min_pulses=arm_min_pulses,
        pause_gcode="M600",
        verbose=False,
    )
    mon.attach_serial(DummySerial())
    return m, mon, logger


def test_double_enable_is_idempotent(monkeypatch):
    """
    Enabling twice should not reset counters or disarm an already-armed monitor.
    This prevents accidental 're-arming' glitches from repeated start-gcode markers.
    """
    m, mon, logger = _make_monitor(monkeypatch, arm_min_pulses=3)

    mon.state.enabled = True
    mon.state.armed = True
    mon.state.motion_pulses_since_reset = 7

    mon._handle_control_marker("M118 A1 filmon:enable")

    assert mon.state.enabled is True
    assert mon.state.armed is True
    assert mon.state.motion_pulses_since_reset == 7


def test_enable_disable_enable_does_not_immediately_jam_without_motion(monkeypatch):
    """
    If arm_min_pulses==0 (armed immediately), enabling should not cause an
    immediate jam before any motion is observed.
    """
    m, mon, logger = _make_monitor(monkeypatch, arm_min_pulses=0, jam_timeout_s=1.0)

    # Make time deterministic: monotonic starts at 100.0 and doesn't advance for this check.
    monkeypatch.setattr(m.time, "monotonic", lambda: 100.0, raising=True)
    monkeypatch.setattr(m.time, "time", lambda: 1000.0, raising=True)

    mon._handle_control_marker("M118 A1 filmon:enable")
    # Sanity: armed immediately
    assert mon.state.armed is True

    # Immediate evaluation should NOT trigger pause
    mon._maybe_jam()
    assert mon.state.latched is False
    assert mon._ser.writes == []


def test_pause_latch_requires_explicit_reset_even_after_motion(monkeypatch):
    """
    After a pause is triggered, the monitor should remain latched until an explicit
    reset marker is received, even if motion resumes.
    """
    m, mon, logger = _make_monitor(monkeypatch, arm_min_pulses=1, jam_timeout_s=1.0)

    # Deterministic time
    t0 = 200.0
    monkeypatch.setattr(m.time, "monotonic", lambda: t0, raising=True)
    monkeypatch.setattr(m.time, "time", lambda: 2000.0, raising=True)

    # Enable + arm (with one pulse)
    mon._handle_control_marker("M118 A1 filmon:enable")
    mon._on_motion_pulse()
    assert mon.state.armed is True

    # Make it look like we haven't seen pulses for longer than the jam timeout.
    monkeypatch.setattr(m.time, "monotonic", lambda: t0 + 10.0, raising=True)
    mon._maybe_jam()
    assert mon.state.latched is True
    assert any(b"M600" in w for w in mon._ser.writes)

    # Motion resumes after the pause; latch should remain set until reset.
    monkeypatch.setattr(m.time, "time", lambda: 2000.0 + 10.0, raising=True)
    mon._on_motion_pulse()
    assert mon.state.latched is True

    # Explicit reset clears latch.
    mon._handle_control_marker("M118 A1 filmon:reset")
    assert mon.state.latched is False


def test_gpio_pulses_are_ignored_after_stop(monkeypatch):
    """
    Late GPIO callbacks after shutdown begins should not mutate state.
    """
    m, mon, logger = _make_monitor(monkeypatch, arm_min_pulses=5)

    before_total = mon.state.motion_pulses_total
    before_since = mon.state.motion_pulses_since_reset
    before_ts = mon.state.last_pulse_ts

    mon.stop()
    mon._on_motion_pulse()

    assert mon.state.motion_pulses_total == before_total
    assert mon.state.motion_pulses_since_reset == before_since
    assert mon.state.last_pulse_ts == before_ts
