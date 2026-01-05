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


# Expose helpers for tests without explicit imports.
builtins.DummyGPIO = DummyGPIO
builtins.DummyDigitalInputDevice = DummyDigitalInputDevice
