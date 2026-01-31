
import types

def test_single_notification_on_jam(monkeypatch):
    calls = []
    class DummyNotifier:
        def send(self, title, message, priority=0):
            calls.append((title, message, priority))

    from filmon.monitor import Monitor, MonitorState
    m = Monitor.__new__(Monitor)
    m.notifier = DummyNotifier()
    m.state = MonitorState(enabled=True, armed=True, latched=False, runout=False,
                           motion_pulses_since_reset=0, motion_pulses_since_arm=0)
    # simulate jam latch
    m._trigger_pause(reason="jam")
    # second call should not notify again if latched
    m.state.latched = True
    m._trigger_pause(reason="jam")

    assert len(calls) == 1

def test_no_notify_when_already_latched(monkeypatch):
    calls = []
    class DummyNotifier:
        def send(self, title, message, priority=0):
            calls.append((title, message, priority))

    from filmon.monitor import Monitor, MonitorState
    m = Monitor.__new__(Monitor)
    m.notifier = DummyNotifier()
    m.state = MonitorState(enabled=True, armed=True, latched=True, runout=False,
                           motion_pulses_since_reset=0, motion_pulses_since_arm=0)

    m._trigger_pause(reason="jam")
    assert calls == []
