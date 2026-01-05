import time
import pytest

from filmon.monitor import FilamentMonitor
from filmon.state import MonitorState
from filmon.logging import JsonLogger


def _mk():
    state = MonitorState()
    logger = JsonLogger(verbose=False)
    mon = FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=26,
        runout_gpio=None,
        runout_active_high=False,
        runout_debounce_s=0.0,
        jam_timeout_s=8.0,
        arm_min_pulses=0,
        pause_gcode="M600",
        verbose=False,
        jam_timeout_adaptive=True,
        jam_timeout_min_s=6.0,
        jam_timeout_max_s=18.0,
        jam_timeout_k=16.0,
        jam_timeout_pps_floor=0.3,
        jam_timeout_ema_halflife_s=3.0,
    )
    mon.enabled = True
    mon.armed = True
    return mon


def test_adaptive_timeout_enabled_flag_is_stored():
    mon = _mk()
    assert mon.jam_timeout_adaptive is True


def test_adaptive_timeout_exceeds_fixed_when_pps_low():
    mon = _mk()
    now = time.monotonic()
    mon._pps_ema = 0.2
    mon._pps_ema_last_ts = now

    eff = mon.jam_timeout_effective_s(now)

    assert eff > 8.0
    assert eff <= 18.0


def test_fixed_timeout_ignores_pps_ema():
    state = MonitorState()
    logger = JsonLogger(verbose=False)
    mon = FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=26,
        runout_gpio=None,
        runout_active_high=False,
        runout_debounce_s=0.0,
        jam_timeout_s=8.0,
        arm_min_pulses=0,
        pause_gcode="M600",
        verbose=False,
        jam_timeout_adaptive=False,
    )
    mon.enabled = True
    mon.armed = True

    mon._pps_ema = 0.01
    mon._pps_ema_last_ts = time.monotonic()

    eff = mon.jam_timeout_effective_s(time.monotonic())
    assert eff == pytest.approx(8.0)