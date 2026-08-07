from __future__ import annotations


# ---------------- Dependencies ----------------
#
# The monitor can be imported without hardware dependencies so unit tests can run
# on any machine (they inject a gpio_factory). Importing this module must stay
# hardware-free: the lgpio pin factory is only instantiated by init_gpio(), which
# the CLI calls before entering any mode that touches real pins.
#
# GPIO_BACKEND describes what is active:
#   "lgpio"            - gpiozero with the lgpio pin factory (set by init_gpio())
#   "gpiozero-default" - gpiozero with its default pin-factory search
#   "stub"             - gpiozero is not installed; DigitalInputDevice is a no-op

GPIO_BACKEND = "stub"

try:
    from gpiozero import DigitalInputDevice, Device
    GPIO_BACKEND = "gpiozero-default"
except ImportError:  # pragma: no cover
    Device = None

    class DigitalInputDevice:  # minimal stub for non-hardware unit tests
        """GPIO stub used when gpiozero is not installed.

        The real implementation is only required when running on hardware.
        """
        def __init__(self, *args, **kwargs):
            self.when_activated = None
            self.when_deactivated = None

try:
    from gpiozero.pins.lgpio import LGPIOFactory
except ImportError:  # pragma: no cover
    LGPIOFactory = None


def init_gpio() -> str:
    """Activate a real GPIO backend and return its name.

    Prefers the lgpio pin factory (Pi 5 / Debian Trixie+) and falls back to
    gpiozero's default pin-factory search when lgpio is unavailable. Returns
    "stub" when gpiozero itself is missing — callers that need real pins must
    refuse to run in that case rather than monitor nothing.
    """
    global GPIO_BACKEND
    if Device is None:
        GPIO_BACKEND = "stub"
        return GPIO_BACKEND
    if LGPIOFactory is not None:
        try:
            # Force lgpio backend (Pi 5 / Debian Trixie+)
            Device.pin_factory = LGPIOFactory()
            GPIO_BACKEND = "lgpio"
            return GPIO_BACKEND
        except Exception:  # pragma: no cover - lgpio wheel present but broken
            pass
    GPIO_BACKEND = "gpiozero-default"
    return GPIO_BACKEND
