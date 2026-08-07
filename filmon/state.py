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
        DISABLED → ENABLED  (filmon:enable / filmon:unarm)
        DISABLED → ARMED    (filmon:arm)
        ENABLED  → ARMED    (filmon:arm)
        ENABLED  → DISABLED (filmon:disable / filmon:reset)
        ARMED    → ENABLED  (filmon:unarm only)
        ARMED    → DISABLED (filmon:disable / filmon:reset)
        any      → DISABLED (filmon:reset also clears latch)

    Edge cases (intentional):
        - filmon:enable while ARMED is a logged no-op; it never disarms.
          Leaving ARMED requires filmon:unarm (or disable/reset).
        - filmon:unarm from DISABLED enables monitoring (lands in ENABLED).
    latched=True is an overlay on ARMED: jam/runout fired, waiting for operator reset/rearm.
    rearm (control-socket command or button long-press) is only honored while latched;
    otherwise the socket returns an error and the button logs rearm_ignored.
    """
    mode: MonitorMode = MonitorMode.DISABLED
    latched: bool = False
    pause_sent_ts: float = 0.0
    pause_delivered: bool = False  # last pause G-code attempt reached the port

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

    # Mirrors the monitor's adaptive-timeout config so status snapshots
    # (dataclasses.asdict) report whether the jam timeout is adaptive.
    jam_timeout_adaptive: bool = False
