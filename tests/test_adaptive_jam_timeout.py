import time
import pytest

from filmon.monitor import FilamentMonitor
from filmon.state import MonitorState
from filmon.logging import JsonLogger

from builtins import DummyGPIO


def _make_monitor(adaptive: bool):
    state = MonitorState()
    logger = JsonLogger(enable_json=False)
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
        jam_timeout_adaptive=adaptive,
        jam_timeout_min_s=6.0,
        jam_timeout_max_s=18.0,
        jam_timeout_k=16.0,
        jam_timeout_pps_floor=0.3,
        jam_timeout_ema_halflife_s=3.0,
        gpio_factory=DummyGPIO,
    )
    mon.enabled = True
    mon.armed = True
    return mon


def test_adaptive_flag_propagates_to_state():
    mon = _make_monitor(adaptive=True)
    assert mon.jam_timeout_adaptive is True
    assert mon.state.jam_timeout_adaptive is True


def test_adaptive_timeout_exceeds_fixed_when_pps_low():
    mon = _make_monitor(adaptive=True)
    now = time.monotonic()
    mon._pps_ema = 0.2
    mon._pps_ema_last_ts = now

    eff = mon._effective_jam_timeout_s(now)
    assert eff > 8.0
    assert eff <= 18.0


def test_fixed_timeout_is_constant():
    mon = _make_monitor(adaptive=False)
    now = time.monotonic()
    mon._pps_ema = 0.01
    mon._pps_ema_last_ts = now

    eff = mon._effective_jam_timeout_s(now)
    assert eff == pytest.approx(8.0)