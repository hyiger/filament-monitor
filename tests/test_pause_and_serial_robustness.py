import queue
import threading
import time

from builtins import DummyGPIO
from filmon.state import MonitorMode, MonitorState


class CapturingLogger:
    """Minimal logger that matches the monitor's .emit(event, **fields) contract."""
    def __init__(self):
        self.events = []

    def emit(self, event: str, **fields):
        self.events.append((event, fields))

    def names(self):
        return [e for e, _ in self.events]


class FlakySerial:
    """DummySerial whose write can be told to raise (simulates a wedged port)."""
    def __init__(self):
        self.writes = []
        self.fail = False

    def write(self, data: bytes):
        if self.fail:
            raise OSError("write failed: port wedged")
        self.writes.append(data.decode(errors="replace"))

    def flush(self):
        pass


class RecordingNotifier:
    """Notifier stub recording every send() call."""
    def __init__(self):
        self.calls = []

    def send(self, title, message, priority=0):
        self.calls.append({"title": title, "message": message, "priority": priority})


class ScriptedSerial:
    """Fake serial port: yields queued lines, raises queued exceptions, then idles."""
    def __init__(self, script):
        # script items: bytes (returned from readline) or Exception (raised).
        self.script = list(script)
        self.closed = False

    def readline(self):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        time.sleep(0.005)
        return b""

    def write(self, data: bytes):
        pass

    def flush(self):
        pass

    def close(self):
        self.closed = True


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
    ser = FlakySerial()
    mon.attach_serial(ser)
    mon.notifier = RecordingNotifier()
    return m, mon, logger, ser


def test_pause_send_failure_latches_notifies_and_retries(monkeypatch):
    """A failed pause write must latch, log, notify, keep the loop alive — and retry."""
    m, mon, logger, ser = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")
    assert mon.state.mode == MonitorMode.ARMED

    # Wedge the port, then let the jam timeout expire via a real main-loop pass.
    ser.fail = True
    t["now"] += 2.0
    mon._loop_once()

    # Latched despite the failed send; delivery is tracked as False.
    assert mon.state.latched is True
    assert mon.state.pause_delivered is False
    assert mon._pause_delivered is False
    assert ser.writes == []

    names = logger.names()
    assert "pause_triggered" in names
    assert "gcode_send_failed" in names
    assert "pause_gcode_failed" in names

    # Notification still attempted, flagged as undelivered, priority 1.
    assert len(mon.notifier.calls) == 1
    assert "(pause G-code send FAILED)" in mon.notifier.calls[0]["message"]
    assert mon.notifier.calls[0]["priority"] == 1

    # The main loop must survive a failed send (no fatal error, no stop).
    assert not mon._stop_evt.is_set()
    assert "monitor_loop_error" not in names

    # Too soon for a retry: nothing sent yet.
    t["now"] += 2.0
    mon._loop_once()
    assert "pause_retry" not in logger.names()
    assert ser.writes == []

    # Port recovers; after the retry interval the pause sequence is delivered.
    ser.fail = False
    t["now"] += 3.5  # >= 5.0 s since the failed attempt
    mon._loop_once()

    assert "pause_retry" in logger.names()
    assert ser.writes == ["M400\n", "M600\n"]
    assert mon.state.pause_delivered is True
    assert mon._pause_delivered is True
    # Delivery on retry does not re-notify.
    assert len(mon.notifier.calls) == 1

    # Once delivered, no further retries occur.
    t["now"] += 10.0
    mon._loop_once()
    assert ser.writes == ["M400\n", "M600\n"]


def test_loop_error_emits_monitor_loop_error_and_stops(monkeypatch):
    """An unexpected exception in the loop body is fatal: logged + stop event set."""
    m, mon, logger, ser = _make_monitor(monkeypatch)

    def boom():
        raise RuntimeError("poisoned handler")

    monkeypatch.setattr(mon, "_maybe_jam", boom, raising=True)

    # Run the loop inline: the first pass raises, is caught, and stops the loop.
    mon._loop()

    assert mon._stop_evt.is_set()
    errors = [f for e, f in logger.events if e == "monitor_loop_error"]
    assert len(errors) == 1
    assert "poisoned handler" in errors[0]["error"]


def test_start_stores_supervisable_loop_thread(monkeypatch):
    """start() must keep the loop thread reference so the CLI can supervise it."""
    m, mon, logger, ser = _make_monitor(monkeypatch)

    mon.start()
    try:
        assert mon._loop_thread is not None
        assert mon._loop_thread.is_alive()
    finally:
        mon.stop()
    mon._loop_thread.join(timeout=2.0)
    assert not mon._loop_thread.is_alive()


def test_runout_callbacks_ignored_after_stop(monkeypatch):
    """Runout edges during shutdown must not touch state or write to serial."""
    m, mon, logger, ser = _make_monitor(monkeypatch)
    mon._handle_control_marker("filmon:arm")

    mon.stop()
    mon._on_runout_asserted()
    assert mon.state.runout_asserted is False
    assert mon.state.latched is False
    assert ser.writes == []

    mon.state.runout_asserted = True
    mon._on_runout_cleared()
    assert mon.state.runout_asserted is True  # untouched after stop


def test_serial_thread_reconnects_after_read_error(monkeypatch):
    """A transient read error must reconnect with backoff, not end the thread."""
    import filmon.serialio as serialio
    monkeypatch.setattr(serialio, "RECONNECT_BACKOFF_S", (0.01,), raising=True)

    first = ScriptedSerial([b"ok before\n", OSError("EMI hiccup")])
    second = ScriptedSerial([b"ok after\n"])

    factory_calls = []

    def factory(port, baud):
        factory_calls.append((port, baud))
        return second

    reattached = []
    logger = CapturingLogger()
    out_q = queue.Queue()
    stop_evt = threading.Event()
    state = MonitorState(serial_connected=True, serial_port="/dev/ttyFAKE", baud=115200)

    t = serialio.SerialThread(
        first,
        out_q,
        stop_evt,
        logger,
        port="/dev/ttyFAKE",
        baud=115200,
        state=state,
        on_reconnect=reattached.append,
        serial_factory=factory,
    )
    t.start()
    try:
        # Line before the error, then a line after the reconnect.
        assert out_q.get(timeout=2.0) == "ok before"
        assert out_q.get(timeout=2.0) == "ok after"

        assert t.is_alive()
        names = logger.names()
        assert "serial_read_error" in names
        assert "serial_reconnected" in names

        # Old port closed, factory used with the configured settings,
        # reopened port handed back for the monitor to swap in.
        assert first.closed is True
        assert factory_calls == [("/dev/ttyFAKE", 115200)]
        assert reattached == [second]
        assert state.serial_connected is True
    finally:
        stop_evt.set()
        t.join(timeout=2.0)
    assert not t.is_alive()


def test_trigger_pause_race_sends_single_pause(monkeypatch):
    """Two threads racing _trigger_pause (jam loop vs runout callback) send one M600."""
    m = load_module()

    for _ in range(300):
        logger = CapturingLogger()
        state = m.MonitorState()
        mon = m.FilamentMonitor(
            state=state,
            logger=logger,
            motion_gpio=26,
            runout_gpio=27,
            runout_active_high=False,
            runout_debounce_s=0.0,
            jam_timeout_s=1.0,
            arm_min_pulses=12,
            pause_gcode="M600",
            verbose=False,
            gpio_factory=DummyGPIO,
        )
        ser = FlakySerial()
        mon.attach_serial(ser)
        mon.notifier = RecordingNotifier()
        mon.state.mode = MonitorMode.ARMED

        barrier = threading.Barrier(2)

        def racer(reason):
            barrier.wait()
            mon._trigger_pause(reason)

        t1 = threading.Thread(target=racer, args=("jam",))
        t2 = threading.Thread(target=racer, args=("runout",))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        m600s = [w for w in ser.writes if w.startswith("M600")]
        assert len(m600s) == 1, f"expected exactly one M600, got writes={ser.writes}"
        assert ser.writes == ["M400\n", "M600\n"]
        assert len(mon.notifier.calls) == 1
        assert logger.names().count("pause_triggered") == 1


class M400OnlyFailsSerial:
    """Serial whose write fails ONLY for M400 (pause line succeeds)."""
    def __init__(self):
        self.writes = []

    def write(self, data: bytes):
        text = data.decode(errors="replace")
        if text.startswith("M400"):
            raise OSError("write failed: M400 dropped")
        self.writes.append(text)

    def flush(self):
        pass


def test_pause_delivered_when_only_m400_fails(monkeypatch):
    """Delivery is judged by the pause line alone: a failed M400 with a
    delivered M600 must NOT trigger retries — each retry would queue another
    filament change (Codex review on #39/#41)."""
    m, mon, logger, notifier = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    mon.attach_serial(M400OnlyFailsSerial())
    t = {"now": 3000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")
    t["now"] += 2.0
    mon._maybe_jam()

    assert mon.state.latched is True
    assert mon.state.pause_delivered is True
    assert [w for w in mon._ser.writes if "M600" in w], "pause line must be sent"
    assert "gcode_send_failed" in logger.names()  # the M400 failure is still logged

    # No retry: advance past the retry interval and run the retry hook.
    writes_before = list(mon._ser.writes)
    t["now"] += 10.0
    mon._maybe_pause_retry()
    assert mon._ser.writes == writes_before
    assert "pause_retry" not in logger.names()


def test_pulse_deque_bounded_while_disabled(monkeypatch):
    """Pulses arriving while DISABLED must still be pruned by the main loop —
    the callback is append-only and no detection path runs in that mode
    (Codex review on #39)."""
    m, mon, logger, notifier = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 5000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:reset")  # DISABLED
    for _ in range(300):
        mon._on_motion_pulse()
        t["now"] += 0.1
    assert len(mon._pulse_times) > 250  # accumulated (no prune ran yet)

    # One loop pass while still DISABLED prunes to the pps window (2 s => ~20).
    mon._loop_once()
    assert len(mon._pulse_times) <= 25


def test_send_gcode_refuses_while_disconnected(monkeypatch):
    """A write racing the reconnect can be buffered by the dying port object
    and discarded on close while being reported delivered. The reader clears
    serial_connected before closing, so _send_gcode must refuse while it is
    False and let the retry path resend after reconnect (Codex round-4 P1)."""
    m, mon, logger, notifier = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 8000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    # Reader has flagged the disconnect; the stale port object still "works".
    mon.state.serial_connected = False
    mon._handle_control_marker("filmon:arm")
    t["now"] += 2.0
    mon._maybe_jam()

    assert mon.state.latched is True
    assert mon.state.pause_delivered is False
    assert mon._ser.writes == []  # nothing handed to the dying port
    assert "gcode_send_failed" in logger.names()

    # Reconnect completes: flag restored, retry delivers the full sequence.
    mon.state.serial_connected = True
    t["now"] += 10.0
    mon._maybe_pause_retry()
    assert mon.state.pause_delivered is True
    assert any("M600" in w for w in mon._ser.writes)


def test_send_gcode_fails_when_disconnect_begins_mid_write(monkeypatch):
    """A disconnect flagged while the write is in flight means the dying port
    may buffer-and-discard the bytes: the post-write recheck must report
    failure so the retry path resends after reconnect (Codex review on #42)."""
    m, mon, logger, notifier = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 9000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    class MidWriteDisconnectSerial:
        def __init__(self, mon):
            self.mon = mon
            self.writes = []
        def write(self, data: bytes):
            self.writes.append(data.decode(errors="replace"))
            # Reader flags the disconnect while this write is in flight.
            self.mon.state.serial_connected = False
        def flush(self):
            pass

    mon.attach_serial(MidWriteDisconnectSerial(mon))
    mon._handle_control_marker("filmon:arm")
    t["now"] += 2.0
    mon._maybe_jam()

    assert mon.state.latched is True
    assert mon.state.pause_delivered is False  # ambiguous write not trusted
    assert "gcode_send_failed" in logger.names()

    # Reconnect restores the flag; the retry delivers for real.
    mon.state.serial_connected = True
    mon._ser.write = lambda data: mon._ser.writes.append(data.decode(errors="replace"))
    t["now"] += 10.0
    mon._maybe_pause_retry()
    assert mon.state.pause_delivered is True
