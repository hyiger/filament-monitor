from __future__ import annotations

VERSION = "1.0.4"

CONTROL_ENABLE = "filmon:enable"
CONTROL_DISABLE = "filmon:disable"
CONTROL_RESET = "filmon:reset"
CONTROL_ARM = "filmon:arm"
CONTROL_UNARM = "filmon:unarm"


USAGE_EXAMPLES = """\
Usage examples:
  # Run normally (printer connected over USB)
  python filament-monitor.py -p /dev/ttyACM0

  # Motion + runout inputs (BCM numbering)
  python filament-monitor.py -p /dev/ttyACM0 --motion-gpio 26 --runout-gpio 27 --runout-enabled --runout-active-high

  # Conservative jam tuning (marker-driven arming)
  python filament-monitor.py -p /dev/ttyACM0 --jam-timeout 8 --stall-thresholds 3,6 --verbose --json

  # Safe dry-run (does not send pause commands)
  python filament-monitor.py --self-test -p /dev/ttyACM0

  # Host/printer diagnostic
  python filament-monitor.py --doctor -p /dev/ttyACM0
"""

