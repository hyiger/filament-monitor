import importlib.util
import sys
import builtins
from pathlib import Path

def load_module():
    script = Path(__file__).resolve().parents[1] / "filament-monitor.py"
    spec = importlib.util.spec_from_file_location("filament_monitor", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["filament_monitor"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

# Expose helper for tests without explicit imports.
builtins.load_module = load_module


class DummyDigitalInputDevice:
    """GPIO stub used by tests.

    Mimics the subset of gpiozero.DigitalInputDevice that the monitor uses:
    - when_activated / when_deactivated callbacks
    - optional close()
    Tests can manually call trigger_* to simulate edges.
    """

    def __init__(self, *args, **kwargs):
        self.when_activated = None
        self.when_deactivated = None

    def trigger_activated(self):
        cb = self.when_activated
        if cb:
            cb()

    def trigger_deactivated(self):
        cb = self.when_deactivated
        if cb:
            cb()

    def close(self):  # pragma: no cover
        return None


class DummyGPIO:
    DigitalInputDevice = DummyDigitalInputDevice


class CapturingLogger:
    """Minimal logger that matches the monitor's .emit(event, **fields) contract.

    Records (event, fields) tuples so tests can assert on emitted events.
    """

    def __init__(self):
        self.events = []

    def emit(self, event: str, **fields):
        self.events.append((event, fields))


class DummySerial:
    """Serial stub capturing bytes written by the monitor (decoded for assertions)."""

    def __init__(self):
        self.writes = []

    def write(self, data: bytes):
        self.writes.append(data.decode(errors="replace"))

    def flush(self):
        pass


# Expose helpers for tests without explicit imports.
builtins.DummyGPIO = DummyGPIO
builtins.DummyDigitalInputDevice = DummyDigitalInputDevice
builtins.CapturingLogger = CapturingLogger
builtins.DummySerial = DummySerial
