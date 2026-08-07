"""Wiring-level GPIO tests: polarity, arm-time runout checks, level
reconciliation, and the GPIO backend guard.

Unlike the other suites (which call the private callbacks directly), these
tests drive physical pin levels through a gpiozero-faithful dummy so the
callback *wiring* itself is under test for both polarities.
"""

import builtins
import importlib
import sys

import pytest

from filmon.state import MonitorMode


class CapturingLogger:
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


class LevelDigitalInputDevice:
    """GPIO stub that simulates pin levels with gpiozero polarity semantics.

    With pull_up=True the pin idles HIGH and "active" means LOW; with
    pull_up=False the pin idles LOW and "active" means HIGH. set_level()
    fires when_activated/when_deactivated on logical transitions, mirroring
    gpiozero's edge dispatch.
    """

    def __init__(self, pin, pull_up=True, **kwargs):
        self.pin = pin
        self.pull_up = pull_up
        self.kwargs = kwargs
        self.when_activated = None
        self.when_deactivated = None
        # Idle at the pulled level: HIGH with a pull-up, LOW with a pull-down.
        self._level_high = bool(pull_up)

    @property
    def is_active(self):
        return (not self._level_high) if self.pull_up else self._level_high

    @property
    def value(self):
        return 1 if self.is_active else 0

    def set_level(self, high: bool):
        """Drive the physical pin level, firing callbacks on logical edges."""
        prev_active = self.is_active
        self._level_high = bool(high)
        if self.is_active and not prev_active:
            if self.when_activated:
                self.when_activated()
        elif prev_active and not self.is_active:
            if self.when_deactivated:
                self.when_deactivated()

    def force_level(self, high: bool):
        """Set the physical level WITHOUT firing callbacks.

        Simulates an edge whose callback was discarded (e.g. by debounce),
        leaving the tracked state stale relative to the pin.
        """
        self._level_high = bool(high)

    def close(self):  # pragma: no cover
        return None


class LevelGPIO:
    DigitalInputDevice = LevelDigitalInputDevice


def _events(logger):
    return [e for e, _ in logger.events]


def _make_monitor(
    monkeypatch,
    *,
    runout_gpio=None,
    runout_active_high=False,
    runout_debounce_s=0.05,
    rearm_button_gpio=None,
    rearm_button_active_high=False,
    **kwargs,
):
    m = load_module()

    logger = CapturingLogger()
    state = m.MonitorState()
    mon = m.FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=26,
        runout_gpio=runout_gpio,
        runout_active_high=runout_active_high,
        runout_debounce_s=runout_debounce_s,
        jam_timeout_s=5.0,
        arm_min_pulses=1,
        pause_gcode="M600",
        rearm_button_gpio=rearm_button_gpio,
        rearm_button_active_high=rearm_button_active_high,
        rearm_button_debounce_s=0.25,
        rearm_button_long_press_s=1.5,
        gpio_factory=LevelGPIO,
        **kwargs,
    )
    mon.attach_serial(DummySerial())
    return m, mon, logger


def _runout_level(active_high: bool, asserted: bool) -> bool:
    """Physical pin level (True=HIGH) for a runout switch state."""
    return asserted == active_high


def _button_level(active_high: bool, pressed: bool) -> bool:
    """Physical pin level (True=HIGH) for a button state."""
    return pressed == active_high


# ---------------- Runout wiring (issue #24) ----------------


@pytest.mark.parametrize("active_high", [False, True], ids=["active_low", "active_high"])
def test_runout_edge_pauses_while_armed(monkeypatch, active_high):
    m, mon, logger = _make_monitor(monkeypatch, runout_gpio=27, runout_active_high=active_high)
    t = {"now": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    # gpiozero normalizes polarity via pull_up: pull-up for active-low wiring,
    # pull-down for active-high wiring.
    assert mon.runout.pull_up is (not active_high)

    mon._handle_control_marker("filmon:arm")
    # Filament present at arm time: no pause.
    assert mon.state.latched is False
    assert mon._ser.writes == []

    # Physical runout edge.
    t["now"] += 0.2
    mon.runout.set_level(_runout_level(active_high, asserted=True))

    assert mon.state.runout_asserted is True
    assert mon.state.latched is True
    assert mon.state.last_trigger == "runout"
    joined = "".join(mon._ser.writes)
    assert "M400" in joined and "M600" in joined
    assert "runout_asserted" in _events(logger)
    assert "pause_triggered" in _events(logger)


@pytest.mark.parametrize("active_high", [False, True], ids=["active_low", "active_high"])
def test_reload_while_armed_does_not_pause(monkeypatch, active_high):
    m, mon, logger = _make_monitor(monkeypatch, runout_gpio=27, runout_active_high=active_high)
    t = {"now": 2000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")

    # Runout pauses (once).
    t["now"] += 0.2
    mon.runout.set_level(_runout_level(active_high, asserted=True))
    assert mon.state.latched is True
    writes_after_runout = list(mon._ser.writes)
    assert "".join(writes_after_runout).count("M600") == 1

    # Operator reloads filament while still armed+latched: must NOT pause again
    # (under the inverted wiring this edge fired _on_runout_asserted).
    t["now"] += 0.2
    mon.runout.set_level(_runout_level(active_high, asserted=False))
    assert mon._ser.writes == writes_after_runout
    assert mon.state.runout_asserted is False
    assert "runout_cleared" in _events(logger)

    # Rearm after the reload: no immediate re-pause, and a fresh runout edge
    # pauses again.
    t["now"] += 0.2
    mon._cmd_rearm()
    assert mon.state.latched is False
    assert mon.state.mode == MonitorMode.ARMED
    assert "".join(mon._ser.writes).count("M600") == 1

    t["now"] += 0.2
    mon.runout.set_level(_runout_level(active_high, asserted=True))
    assert mon.state.latched is True
    assert "".join(mon._ser.writes).count("M600") == 2


# ---------------- Rearm button wiring (issue #24) ----------------


@pytest.mark.parametrize("active_high", [False, True], ids=["active_low", "active_high"])
def test_button_long_press_triggers_rearm(monkeypatch, active_high):
    m, mon, logger = _make_monitor(
        monkeypatch, rearm_button_gpio=25, rearm_button_active_high=active_high
    )
    t = {"now": 3000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    assert mon.rearm_button.pull_up is (not active_high)

    # Latched after a fault.
    mon.state.mode = MonitorMode.ARMED
    mon.state.latched = True
    mon.state.motion_pulses_since_reset = 10
    mon.state.motion_pulses_since_arm = 5

    # Press, hold past the long-press threshold, release.
    mon.rearm_button.set_level(_button_level(active_high, pressed=True))
    t["now"] += 2.0
    mon.rearm_button.set_level(_button_level(active_high, pressed=False))

    assert mon.state.latched is False
    assert mon.state.mode == MonitorMode.ARMED
    assert mon.state.motion_pulses_since_reset == 0
    assert mon.state.motion_pulses_since_arm == 0
    assert "rearmed" in _events(logger)


@pytest.mark.parametrize("active_high", [False, True], ids=["active_low", "active_high"])
def test_button_quick_tap_triggers_reset(monkeypatch, active_high):
    m, mon, logger = _make_monitor(
        monkeypatch, rearm_button_gpio=25, rearm_button_active_high=active_high
    )
    t = {"now": 4000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon.state.mode = MonitorMode.ARMED
    mon.state.latched = True

    # Quick tap: press then release before the long-press threshold.
    mon.rearm_button.set_level(_button_level(active_high, pressed=True))
    t["now"] += 0.4
    mon.rearm_button.set_level(_button_level(active_high, pressed=False))

    assert mon.state.mode == MonitorMode.DISABLED
    assert mon.state.latched is False
    assert "reset" in _events(logger)


# ---------------- Arming checks runout (issue #33) ----------------


@pytest.mark.parametrize("active_high", [False, True], ids=["active_low", "active_high"])
def test_arm_with_runout_already_asserted_pauses_immediately(monkeypatch, active_high):
    m, mon, logger = _make_monitor(monkeypatch, runout_gpio=27, runout_active_high=active_high)
    t = {"now": 5000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    # Runout occurs while unarmed (e.g. during the first layer): tracked, no pause.
    mon.runout.set_level(_runout_level(active_high, asserted=True))
    assert mon.state.runout_asserted is True
    assert mon._ser.writes == []

    # Arming must consult the standing runout and pause immediately.
    t["now"] += 0.2
    mon._handle_control_marker("filmon:arm")
    assert mon.state.latched is True
    assert mon.state.last_trigger == "runout"
    joined = "".join(mon._ser.writes)
    assert "M400" in joined and "M600" in joined
    assert "runout_asserted" in _events(logger)


def test_rearm_with_runout_still_asserted_pauses_again(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, runout_gpio=27, runout_active_high=False)
    t = {"now": 6000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")
    t["now"] += 0.2
    mon.runout.set_level(_runout_level(False, asserted=True))
    assert mon.state.latched is True
    assert "".join(mon._ser.writes).count("M600") == 1

    # Operator rearms WITHOUT reloading: the standing runout must not be
    # forgotten (rearm no longer clears runout_asserted) and must re-pause.
    t["now"] += 0.2
    mon._cmd_rearm()
    assert mon.state.runout_asserted is True
    assert mon.state.latched is True
    assert mon.state.last_trigger == "runout"
    assert "".join(mon._ser.writes).count("M600") == 2


# ---------------- Debounce settle / level reconciliation (issue #33) ----------------


def test_reconcile_runout_syncs_swallowed_assert_edge_and_pauses(monkeypatch):
    m, mon, logger = _make_monitor(
        monkeypatch, runout_gpio=27, runout_active_high=False, runout_debounce_s=0.05
    )
    t = {"now": 7000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")
    assert mon.state.latched is False

    # Simulate a chatter burst whose final (asserting) edge fell inside the
    # debounce window: the pin is asserted but the callback was discarded.
    mon.runout.force_level(_runout_level(False, asserted=True))
    mon._last_runout_edge = t["now"]

    # Still inside the quiet period: reconciliation must not act yet.
    mon._reconcile_runout()
    assert mon.state.runout_asserted is False
    assert mon.state.latched is False
    assert mon._ser.writes == []

    # Quiet period elapsed: level is re-sampled, state synced, pause fired.
    t["now"] += 0.06
    mon._reconcile_runout()
    assert mon.state.runout_asserted is True
    assert mon.state.latched is True
    assert mon.state.last_trigger == "runout"
    assert "runout_asserted" in _events(logger)
    assert "M600" in "".join(mon._ser.writes)


def test_reconcile_runout_syncs_swallowed_clear_edge_without_pause(monkeypatch):
    m, mon, logger = _make_monitor(
        monkeypatch, runout_gpio=27, runout_active_high=False, runout_debounce_s=0.05
    )
    t = {"now": 8000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:enable")

    # Real runout edge passes debounce and is tracked while unarmed.
    mon.runout.set_level(_runout_level(False, asserted=True))
    assert mon.state.runout_asserted is True

    # The clearing edge of a reload is swallowed by debounce: state goes stale.
    t["now"] += 0.2
    mon.runout.force_level(_runout_level(False, asserted=False))
    mon._last_runout_edge = t["now"]

    t["now"] += 0.06
    mon._reconcile_runout()
    assert mon.state.runout_asserted is False
    assert mon.state.latched is False
    assert mon._ser.writes == []


# ---------------- GPIO backend guard (issue #30) ----------------


def test_gpio_import_without_gpiozero_yields_stub_backend(monkeypatch):
    """Reload filmon.gpio with gpiozero imports failing: stub backend, inert device."""
    import filmon

    orig_mod = sys.modules.get("filmon.gpio")
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "gpiozero" or name.startswith("gpiozero."):
            raise ImportError(f"simulated missing module: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "filmon.gpio", raising=False)
    try:
        fresh = importlib.import_module("filmon.gpio")
        assert fresh.GPIO_BACKEND == "stub"
        assert fresh.Device is None
        assert fresh.init_gpio() == "stub"
        # The stub device is constructible but inert.
        dev = fresh.DigitalInputDevice(26, pull_up=True)
        assert dev.when_activated is None
        assert dev.when_deactivated is None
    finally:
        sys.modules.pop("filmon.gpio", None)
        if orig_mod is not None:
            sys.modules["filmon.gpio"] = orig_mod
            filmon.gpio = orig_mod


def test_init_gpio_backend_selection(monkeypatch):
    import filmon.gpio as fgpio

    # Restore the module-level flag after each mutation below.
    monkeypatch.setattr(fgpio, "GPIO_BACKEND", fgpio.GPIO_BACKEND, raising=True)

    class FakeDevice:
        pin_factory = None

    class FakeFactory:
        pass

    # gpiozero missing entirely -> stub.
    monkeypatch.setattr(fgpio, "Device", None, raising=True)
    monkeypatch.setattr(fgpio, "LGPIOFactory", None, raising=True)
    assert fgpio.init_gpio() == "stub"
    assert fgpio.GPIO_BACKEND == "stub"

    # gpiozero present but lgpio missing -> default pin-factory search.
    monkeypatch.setattr(fgpio, "Device", FakeDevice, raising=True)
    assert fgpio.init_gpio() == "gpiozero-default"

    # lgpio available -> factory instantiated and forced.
    monkeypatch.setattr(fgpio, "LGPIOFactory", FakeFactory, raising=True)
    assert fgpio.init_gpio() == "lgpio"
    assert isinstance(FakeDevice.pin_factory, FakeFactory)


def test_cli_refuses_to_start_on_stub_backend(monkeypatch, capsys):
    m = load_module()
    import filmon.cli as cli

    calls = {"doctor": 0}
    monkeypatch.setattr(cli, "serial", object(), raising=True)  # pretend pyserial is present
    monkeypatch.setattr(cli, "init_gpio", lambda: "stub", raising=True)
    monkeypatch.setattr(cli, "run_doctor", lambda args: calls.__setitem__("doctor", calls["doctor"] + 1), raising=True)

    # Normal mode: refuse with a clear error and non-zero exit.
    monkeypatch.setattr(sys, "argv", ["filament-monitor.py", "-p", "/dev/ttyFAKE"], raising=True)
    assert cli.main() == 2
    assert "GPIO" in capsys.readouterr().err

    # Doctor mode needs real pins too: guarded before run_doctor.
    monkeypatch.setattr(sys, "argv", ["filament-monitor.py", "--doctor", "-p", "/dev/ttyFAKE"], raising=True)
    assert cli.main() == 2
    assert "GPIO" in capsys.readouterr().err
    assert calls["doctor"] == 0
