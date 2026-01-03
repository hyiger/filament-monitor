from __future__ import annotations

import json
import time
from typing import Optional

class JsonLogger:
    """Minimal structured logger.

    Emits single-line JSON events for state transitions (arming, jam, runout, pause)
    so logs are easy to grep and machine-parse."""
    def __init__(self, enable_json: bool):
        """Create a JSON logger.

        Args:
            stream: A file-like object (defaults to stdout) used for event output.
        """
        self.enable_json = enable_json

    def emit(self, event: str, **fields):
        """Emit a JSON event with a name and optional key/value fields."""
        t = time.time()
        # ts: float seconds since epoch (sub-second resolution). ts_iso is a human-friendly local timestamp with milliseconds.
        ts_iso = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t)) + f'.{int((t - int(t))*1000):03d}'
        payload = {"ts": t, "ts_iso": ts_iso, "event": event, **fields}
        if self.enable_json:
            print(json.dumps(payload, sort_keys=True), flush=True)
        else:
            t = time.time()
            ms = int((t - int(t)) * 1000)
            msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))}.{ms:03d}] {event}"
            if fields:
                msg += " " + " ".join(f"{k}={v}" for k, v in fields.items())
            print(msg, flush=True)


