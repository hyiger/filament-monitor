from __future__ import annotations

import threading

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    serial = None

from .logging import JsonLogger

class SerialThread(threading.Thread):
    """Background serial reader.

    Continuously reads lines from the printer's serial port and forwards them to the
    monitor for control-marker handling and (optional) diagnostics."""
    def __init__(self, ser, out_q, stop_evt, logger, verbose: bool = False):
        """Create the serial reader thread.

        Args:
            ser: An open pyserial Serial instance.
            on_line: Callback invoked with each decoded line (str).
            logger: JsonLogger for emitting serial-related events (optional).
        """
        super().__init__(daemon=True)
        self.ser = ser
        self.out_q = out_q
        self.stop_evt = stop_evt
        self.logger = logger
        self.verbose = bool(verbose)

    def run(self):
        """Thread entry point. Reads serial lines until stopped."""
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
                break


