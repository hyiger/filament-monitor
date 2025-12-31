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
