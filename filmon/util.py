from __future__ import annotations

import time


def now_s() -> float:
    """Monotonic clock in seconds."""
    return time.monotonic()
