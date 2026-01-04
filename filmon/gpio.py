from __future__ import annotations


# ---------------- Dependencies ----------------
#
# The monitor can be imported without hardware dependencies so unit tests can run
# on any machine. Hardware-specific imports are required at runtime when GPIO/serial
# features are actually used.

try:
    from gpiozero import DigitalInputDevice, Device
    from gpiozero.pins.lgpio import LGPIOFactory
    # Force lgpio backend (Pi 5 / Debian Trixie+)
    Device.pin_factory = LGPIOFactory()
except ImportError:  # pragma: no cover
    Device = None
    LGPIOFactory = None

    class DigitalInputDevice:  # minimal stub for non-hardware unit tests
        """GPIO stub used when gpiozero is not installed.

        The real implementation is only required when running on hardware.
        """
        def __init__(self, *args, **kwargs):
            self.when_activated = None
            self.when_deactivated = None




