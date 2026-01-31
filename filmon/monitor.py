from __future__ import annotations

import time
from dataclasses import asdict

from filmon.config import (
    get_bool_env,
    get_float_env,
    get_notifier_config,
)
from filmon.notify import Notifier


class Monitor:
    def __init__(self, *args, **kwargs):
        self.enabled = False
        self.armed = False
        self.latched = False
        self.runout_latched = False
        self.motion_pulses_since_reset = 0
        self.last_motion_ts = None

        notify_cfg = get_notifier_config()
        self.notifier = Notifier(
            enabled=notify_cfg["enabled"],
            pushover_token=notify_cfg["pushover_token"],
            pushover_user=notify_cfg["pushover_user"],
        )

    def status(self):
        return {
            "ok": True,
            "state": asdict(self),
        }

    def _handle_jam(self):
        if self.latched:
            return
        self.latched = True
        self._pause_printer()
        self.notifier.send(
            title="Filament Monitor",
            message="🚨 Filament jam detected — print paused (M600)",
            priority=1,
        )

    def _handle_runout(self):
        if self.runout_latched:
            return
        self.runout_latched = True
        self._pause_printer()
        self.notifier.send(
            title="Filament Monitor",
            message="📭 Filament runout detected — print paused",
            priority=1,
        )
