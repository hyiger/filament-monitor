from __future__ import annotations

import threading

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    serial = None

from .logging import JsonLogger

# Reconnect backoff schedule in seconds; the last value repeats (1, 2, 5, 5, ...).
RECONNECT_BACKOFF_S = (1.0, 2.0, 5.0)


def _default_serial_factory(port: str, baud: int):
    """Open a fresh pyserial port with the same settings the CLI uses."""
    if serial is None:  # pragma: no cover
        raise RuntimeError("pyserial is not installed")
    return serial.Serial(port, baud, timeout=0.25, write_timeout=2.0)


class SerialThread(threading.Thread):
    """Background serial reader with reconnect.

    Continuously reads lines from the printer's serial port and forwards them to
    the monitor for control-marker handling and (optional) diagnostics. On a read
    error the port is closed and reopened with backoff instead of giving up, so a
    transient USB/EMI hiccup does not end protection for the rest of the print.
    The thread only exits when stop_evt is set."""
    def __init__(
        self,
        ser,
        out_q,
        stop_evt,
        logger,
        verbose: bool = False,
        port: str = "",
        baud: int = 0,
        state=None,
        on_reconnect=None,
        serial_factory=None,
        ser_lock=None,
    ):
        """Create the serial reader thread.

        Args:
            ser: An open pyserial Serial instance.
            out_q: Queue that decoded lines are put into.
            stop_evt: Threading event; the thread exits when this is set.
            logger: JsonLogger for emitting serial-related events.
            port: Device path used to reopen the port after a read error.
            baud: Baud rate used to reopen the port.
            state: Optional MonitorState; serial_connected is kept truthful
                while disconnected/reconnected.
            on_reconnect: Optional callback invoked with the reopened Serial
                instance so the monitor can swap it in under its write lock.
            serial_factory: Optional callable (port, baud) -> Serial used to
                reopen the port (tests inject fakes here).
            ser_lock: The monitor's serial write lock. Closing the dead port
                takes it so a close can never interleave a pause write.
        """
        super().__init__(daemon=True)
        self.ser = ser
        self.out_q = out_q
        self.stop_evt = stop_evt
        self.logger = logger
        self.verbose = bool(verbose)
        self.port = port
        self.baud = baud
        self.state = state
        self.on_reconnect = on_reconnect
        self.serial_factory = serial_factory or _default_serial_factory
        self.ser_lock = ser_lock

    def run(self):
        """Thread entry point. Reads serial lines until stopped, reconnecting on errors."""
        while not self.stop_evt.is_set():
            try:
                line = self.ser.readline()
                if not line:
                    continue
                text = line.decode("utf-8", errors="replace").strip()
                self.out_q.put(text)
            except Exception as e:
                try:
                    self.logger.emit("serial_read_error", error=str(e))
                except Exception:
                    pass
                if not self._reconnect():
                    break  # stop requested while reconnecting

    def _set_connected(self, connected: bool):
        """Keep state.serial_connected truthful for filmonctl status."""
        if self.state is not None:
            self.state.serial_connected = connected

    def _reconnect(self) -> bool:
        """Close the dead port and reopen it with backoff.

        Returns True once reconnected; False if stop_evt was set first."""
        self._set_connected(False)
        # Close under the monitor's write lock: closing mid-_send_gcode would
        # otherwise tear a pause write (undefined in pyserial) and leave the
        # M400/M600 sequence ambiguously delivered.
        if self.ser_lock is not None:
            with self.ser_lock:
                try:
                    self.ser.close()
                except Exception:
                    pass
        else:
            try:
                self.ser.close()
            except Exception:
                pass

        attempt = 0
        while not self.stop_evt.is_set():
            delay_s = RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)]
            # Interruptible backoff: returns True immediately if stop is requested.
            if self.stop_evt.wait(delay_s):
                return False
            attempt += 1
            try:
                new_ser = self.serial_factory(self.port, self.baud)
            except Exception as e:
                try:
                    self.logger.emit("serial_reconnect_failed", error=str(e), attempt=attempt)
                except Exception:
                    pass
                continue

            self.ser = new_ser
            self._set_connected(True)
            try:
                self.logger.emit("serial_reconnected", port=self.port, attempt=attempt)
            except Exception:
                pass
            # Hand the reopened port to the monitor so writes (pause retry!)
            # go to a live port. The monitor swaps it under its write lock.
            if self.on_reconnect is not None:
                try:
                    self.on_reconnect(new_ser)
                except Exception:
                    pass
            return True
        return False
