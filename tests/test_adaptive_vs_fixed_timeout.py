import time
import pytest

from filmon.monitor import FilamentMonitor


def _make_monitor(**kw):
    mon = FilamentMonitor(**kw)
    # Put the monitor into a valid operational state
    mon.enabled = True
    mon.armed = True
    return mon


def test_adaptive_timeout_exceeds_fixed_when_pps_low():
    mon = _make_monitor(
        jam_timeout_s=8.0,
        jam_timeout_adaptive=True,
        jam_timeout_min_s=6.0,
        jam_timeout_max_s=18.0,
        jam_timeout_k=16.0,
        jam_timeout_pps_floor=0.3,
        jam_timeout_ema_halflife_s=3.0,
        arm_grace_pulses=0,
        arm_grace_s=0.0,
    )

    now = time.monotonic()
    mon._pps_ema = 0.15
    mon._pps_ema_last_ts = now

    eff = mon.jam_timeout_effective_s(now)

    assert eff > 8.0
    assert eff <= 18.0


def test_fixed_timeout_ignores_pps_ema():
    mon = _make_monitor(
        jam_timeout_s=8.0,
        jam_timeout_adaptive=False,
        jam_timeout_min_s=6.0,
        jam_timeout_max_s=18.0,
        jam_timeout_k=16.0,
        jam_timeout_pps_floor=0.3,
        jam_timeout_ema_halflife_s=3.0,
        arm_grace_pulses=0,
        arm_grace_s=0.0,
    )

    now = time.monotonic()
    mon._pps_ema = 0.01
    mon._pps_ema_last_ts = now

    eff = mon.jam_timeout_effective_s(now)

    assert eff == pytest.approx(8.0)