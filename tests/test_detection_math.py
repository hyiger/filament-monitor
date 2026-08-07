"""Detection math tests: EMA decay, timeout clamps, grace gate, enable guard.

Covers the adaptive EMA closed form and its dt==0 stability (issue #27), the
post-arm grace gate with single-criterion configs (issue #28), and the guard
that keeps a stray `filmon:enable` from demoting ARMED (issue #35).
"""

import math

import pytest

from builtins import DummyGPIO
from filmon.state import MonitorMode

HALFLIFE_S = 3.0


class CapturingLogger:
    """Minimal logger that matches the monitor's .emit(event, **fields) contract."""
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


def _make_monitor(monkeypatch, jam_timeout_s=1.0, **kwargs):
    m = load_module()

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
        gpio_factory=DummyGPIO,
        **kwargs,
    )
    mon.attach_serial(DummySerial())
    return m, mon, logger


def _expected_ema(prev: float, pps_now: float, dt: float) -> float:
    """Closed-form single EMA step: alpha = 1 - exp(-dt * ln2 / halflife)."""
    alpha = 1.0 - math.exp(-dt * math.log(2.0) / HALFLIFE_S)
    return (1.0 - alpha) * prev + alpha * pps_now


# ---------------- EMA math ----------------


def test_ema_decay_matches_closed_form(monkeypatch):
    """Each EMA update equals (1-a)*prev + a*pps_now for a known dt sequence."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_adaptive=True,
        jam_timeout_ema_halflife_s=HALFLIFE_S,
        pulse_window_s=2.0,
    )
    t = {"now": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    # First update seeds the EMA with the instantaneous pps.
    mon._on_motion_pulse()
    expected = mon._pps(t["now"])
    assert mon._update_pps_ema(t["now"]) == pytest.approx(expected, abs=1e-9)

    for dt in (0.4, 0.7, 1.3, 2.0):
        t["now"] += dt
        mon._on_motion_pulse()
        pps_now = mon._pps(t["now"])
        expected = _expected_ema(expected, pps_now, dt)
        assert mon._update_pps_ema(t["now"]) == pytest.approx(expected, abs=1e-9)


def test_ema_unchanged_on_zero_dt_double_call(monkeypatch):
    """A second update at the same timestamp must not snap the EMA to raw pps."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_adaptive=True,
        jam_timeout_ema_halflife_s=HALFLIFE_S,
    )
    t = {"now": 2000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    # Smoothed EMA distinct from the instantaneous pps (0.0: no pulses).
    mon._pps_ema = 1.78
    mon._pps_ema_last_ts = t["now"]

    assert mon._update_pps_ema(t["now"]) == pytest.approx(1.78, abs=1e-12)
    assert mon._update_pps_ema(t["now"]) == pytest.approx(1.78, abs=1e-12)
    assert mon._pps_ema == pytest.approx(1.78, abs=1e-12)


def test_hb_emit_does_not_snap_stored_ema(monkeypatch):
    """The heartbeat performs a single EMA update; the stored EMA decays smoothly."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_adaptive=True,
        jam_timeout_ema_halflife_s=HALFLIFE_S,
        breadcrumb_interval_s=2.0,
    )
    t = {"now": 5000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")
    mon._pps_ema = 2.0
    mon._pps_ema_last_ts = t["now"]

    # Cross a heartbeat boundary with no pulses in the window (raw pps = 0).
    t["now"] += 2.0
    mon._next_hb_ts = t["now"]
    expected = _expected_ema(2.0, 0.0, 2.0)

    mon._maybe_breadcrumbs()

    hb_events = [fields for event, fields in logger.events if event == "hb"]
    assert len(hb_events) == 1
    hb = hb_events[0]
    # Stored EMA decays smoothly instead of snapping to the instantaneous pps.
    assert mon._pps_ema == pytest.approx(expected, abs=1e-9)
    assert hb["pps_ema"] == pytest.approx(round(expected, 3))
    # The logged effective timeout is derived from the same single EMA update.
    assert hb["jam_timeout_effective_s"] == pytest.approx(
        round(mon._effective_jam_timeout_from(expected), 3)
    )


# ---------------- Timeout clamps ----------------


def test_effective_timeout_min_clamp_when_pps_high(monkeypatch):
    """High pps => raw k/pps below the floor => clamps up to jam_timeout_min_s."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_s=8.0,
        jam_timeout_adaptive=True,
        jam_timeout_min_s=6.0,
        jam_timeout_max_s=18.0,
        jam_timeout_k=16.0,
        jam_timeout_pps_floor=0.3,
        jam_timeout_ema_halflife_s=HALFLIFE_S,
    )
    t = {"now": 3000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    # 16 / 10.0 = 1.6 s raw -> min clamp. The dt==0 update inside
    # _effective_jam_timeout_s must leave the seeded EMA intact.
    mon._pps_ema = 10.0
    mon._pps_ema_last_ts = t["now"]
    assert mon._effective_jam_timeout_s(t["now"]) == pytest.approx(6.0)
    assert mon._effective_jam_timeout_from(10.0) == pytest.approx(6.0)


def test_effective_timeout_max_clamp_when_pps_zero(monkeypatch):
    """pps -> 0 => denom clamps at pps floor => timeout clamps to jam_timeout_max_s."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_s=8.0,
        jam_timeout_adaptive=True,
        jam_timeout_min_s=6.0,
        jam_timeout_max_s=18.0,
        jam_timeout_k=16.0,
        jam_timeout_pps_floor=0.3,
        jam_timeout_ema_halflife_s=HALFLIFE_S,
    )
    # 16 / max(0.3, 0.0) = 53.3 s raw -> max clamp.
    assert mon._effective_jam_timeout_from(0.0) == pytest.approx(18.0)


# ---------------- Post-arm grace gate ----------------


def test_grace_time_only_gate_suppresses_until_elapsed(monkeypatch):
    """Configuring only arm_grace_s must actually gate (regression: inert gate)."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_s=1.0,
        arm_grace_pulses=0,
        arm_grace_s=12.0,
    )
    t = {"now": 100.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")

    # Beyond the jam timeout but inside the grace window: gate must hold
    # (the unset pulse criterion used to release it immediately).
    t["now"] += 2.0
    mon._maybe_jam()
    assert mon.state.latched is False
    assert mon._ser.writes == []

    # Past the grace window the gate releases and the jam latches.
    t["now"] += 10.5
    mon._maybe_jam()
    assert mon.state.latched is True
    assert any("M600" in w for w in mon._ser.writes)


def test_grace_pulses_only_gate_cannot_suppress_forever(monkeypatch):
    """A pulses-only gate holds early but must release after the effective timeout."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_s=2.0,
        arm_grace_pulses=12,
        arm_grace_s=0.0,
    )
    t = {"now": 200.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")

    # Inside the effective timeout with too few pulses: gate holds.
    t["now"] += 1.9
    mon._maybe_jam()
    assert mon.state.latched is False

    # No pulses ever arrive: the gate must still release once the effective
    # jam timeout elapses — a pulses-only config cannot mask a dead extruder.
    t["now"] += 0.2
    mon._maybe_jam()
    assert mon.state.latched is True


def test_grace_both_criteria_time_releases_with_zero_pulses(monkeypatch):
    """With both criteria set, the time criterion alone releases the gate."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_s=1.0,
        arm_grace_pulses=12,
        arm_grace_s=12.0,
    )
    t = {"now": 300.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")

    t["now"] += 2.0
    mon._maybe_jam()
    assert mon.state.latched is False

    t["now"] += 10.5
    mon._maybe_jam()
    assert mon.state.latched is True


def test_grace_pulse_criterion_releases_before_time(monkeypatch):
    """The pulse criterion alone releases the gate well before the time criterion."""
    m, mon, logger = _make_monitor(
        monkeypatch,
        jam_timeout_s=1.0,
        arm_grace_pulses=3,
        arm_grace_s=60.0,
    )
    t = {"now": 400.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")

    # Three quick pulses satisfy the pulse criterion far ahead of the time one.
    for _ in range(3):
        t["now"] += 0.1
        mon._on_motion_pulse()
    assert mon.state.motion_pulses_since_arm == 3

    # Silence beyond the jam timeout, still deep inside the 60 s grace window:
    # the pulse release lets the jam latch.
    t["now"] += 1.5
    mon._maybe_jam()
    assert mon.state.latched is True
    assert any("M600" in w for w in mon._ser.writes)


# ---------------- Enable guard ----------------


def test_enable_while_armed_is_ignored(monkeypatch):
    """A stray filmon:enable must not demote ARMED -> ENABLED (issue #35)."""
    m, mon, logger = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 500.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")
    assert mon.state.mode == MonitorMode.ARMED

    mon._handle_control_marker("M118 A1 filmon:enable")
    assert mon.state.mode == MonitorMode.ARMED
    event, fields = logger.events[-1]
    assert event == "enabled"
    assert fields.get("ignored") == "already_armed"

    # Detection is still live: the stray enable did not disarm it.
    t["now"] += 2.0
    mon._maybe_jam()
    assert mon.state.latched is True


def test_enable_from_disabled_and_enabled_unchanged(monkeypatch):
    """Enable from DISABLED transitions to ENABLED; from ENABLED it is idempotent."""
    m, mon, logger = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 600.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    assert mon.state.mode == MonitorMode.DISABLED
    mon._handle_control_marker("filmon:enable")
    assert mon.state.mode == MonitorMode.ENABLED
    event, fields = logger.events[-1]
    assert event == "enabled"
    assert "ignored" not in fields

    mon._handle_control_marker("filmon:enable")
    assert mon.state.mode == MonitorMode.ENABLED
    event, fields = logger.events[-1]
    assert event == "enabled"
    assert "ignored" not in fields
