from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MonitorMode(str, Enum):
    """Operating mode for the filament monitor.

    Members are plain strings (str + Enum) so dataclasses.asdict() and
    json.dumps() serialize them without a custom encoder.
    """
    
    DISABLED = "disabled"  # monitoring off; motion/runout checks ignored
    ENABLED  = "enabled"   # monitoring on but unarmed; safe during travel/heatup
    ARMED    = "armed"     # armed; jam/runout conditions can trigger a pause


@dataclass


class MonitorState:
    """Holds mutable runtime state for the monitor.

    This is the shared state updated by GPIO callbacks, the serial reader, and the
    main monitoring loop. It includes a mode flag, a latch flag, and timing/pulse
    counters used for jam and runout decisions.

    mode transitions:
        DISABLED → ENABLED  (filmon:enable)
        DISABLED → ARMED    (filmon:arm)
        ENABLED  → ARMED    (filmon:arm)
        ENABLED  → DISABLED (filmon:disable / filmon:reset)
        ARMED    → ENABLED  (filmon:unarm)
        ARMED    → DISABLED (filmon:disable / filmon:reset)
        any      → DISABLED (filmon:reset also clears latch)
    latched=True is an overlay on ARMED: jam/runout fired, waiting for operator reset/rearm.
    """
    mode: MonitorMode = MonitorMode.DISABLED
    latched: bool = False
    pause_sent_ts: float = 0.0
    last_trigger: str = ""
    last_trigger_ts: float = 0.0

    motion_pulses_total: int = 0
    motion_pulses_since_reset: int = 0
    last_pulse_ts: float = 0.0

    motion_pulses_since_arm: int = 0
    arm_ts: float = 0.0

    runout_asserted: bool = False
    serial_connected: bool = False
    serial_port: str = ""
    baud: int = 0
