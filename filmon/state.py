from __future__ import annotations

from dataclasses import dataclass

@dataclass


class MonitorState:
    """Holds mutable runtime state for the monitor.

    This is the shared state updated by GPIO callbacks, the serial reader, and the
    main monitoring loop. It includes arming/enabled/latch flags and timing/pulse
    counters used for jam and runout decisions."""
    enabled: bool = False
    armed: bool = False
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


