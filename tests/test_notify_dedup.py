
import time


def _mk_monitor(enabled=True, armed=True, latched=False):
    from filmon.monitor import FilamentMonitor
    from filmon.state import MonitorState

    class DummyNotifier:
        def __init__(self):
            self.calls=[]
        def send(self, title, message, priority=0):
            self.calls.append((title, message, priority))

    # Create a minimal instance without running full __init__ (avoids serial/gpio deps).
    m = FilamentMonitor.__new__(FilamentMonitor)
    m.notifier = DummyNotifier()
    m.pause_gcode = "M600"

    # minimal logger with emit()
    class DummyLogger:
        def emit(self, *a, **k):
            pass
    m.logger = DummyLogger()

    # required helpers
    m._ser_lock = None
    m._send_gcode = lambda g: None
    m._pps = lambda now: 0.0

    st = MonitorState(enabled=enabled, armed=armed, latched=latched)
    st.last_pulse_ts = time.time()
    st.motion_pulses_since_reset = 0
    st.motion_pulses_since_arm = 0
    m.state = st

    return m


def test_single_notification_on_jam_then_no_duplicate_when_latched():
    m = _mk_monitor(enabled=True, armed=True, latched=False)

    m._trigger_pause(reason="jam")
    assert len(m.notifier.calls) == 1

    # Calling again while already latched should not generate an additional notification.
    m.state.latched = True
    m._trigger_pause(reason="jam")
    assert len(m.notifier.calls) == 1


def test_no_notify_when_already_latched():
    m = _mk_monitor(enabled=True, armed=True, latched=True)
    m._trigger_pause(reason="jam")
    assert m.notifier.calls == []
