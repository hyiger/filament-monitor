import os
import pty
import sys
import time
import select
import subprocess
from pathlib import Path

import pytest

serial = pytest.importorskip("serial")


def _read_available(fd: int, timeout_s: float = 0.2) -> bytes:
    """Read whatever is available on an fd without blocking too long."""
    end = time.time() + timeout_s
    chunks = []
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
        if len(data) < 4096:
            break
    return b"".join(chunks)


def _write_line(fd: int, line: str) -> None:
    os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8"))


@pytest.mark.integration
def test_virtual_serial_prusa_like_jam_triggers_pause(tmp_path: Path):
    """Simulate a Marlin/Prusa-like printer over a PTY and assert pause_gcode is sent.

    This test must **never hang**. We always terminate the monitor subprocess and collect logs via
    communicate(timeout=...) rather than blocking on stdout.read().
    """
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "filament-monitor.py"
    assert script.exists(), f"Missing script at {script}"

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)

    proc = subprocess.Popen(
        [
            sys.executable,
            str(script),
            "--port",
            slave_path,
            "--baud",
            "115200",
            "--arm-min-pulses",
            "0",
            "--jam-timeout",
            "1.0",
            "--pause-gcode",
            "M600",
            "--no-banner",
        ],
        cwd=str(repo_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    pause_seen = False
    out = ""

    try:
        # Fake printer boot + ok responses
        _write_line(master_fd, "start")
        _write_line(master_fd, "echo:Marlin 2.x (Prusa-like)")
        _write_line(master_fd, "echo:Machine Type: Core One (simulated)")
        _write_line(master_fd, "ok")

        time.sleep(0.4)
        _ = _read_available(master_fd, 0.2)

        # Markers
        _write_line(master_fd, "M118 A1 filmon:reset")
        _write_line(master_fd, "ok")
        _write_line(master_fd, "M118 A1 filmon:enable")
        _write_line(master_fd, "ok")

        # Some extrusion moves (enough to make "jam expected" meaningful)
        _write_line(master_fd, "G92 E0")
        _write_line(master_fd, "ok")
        _write_line(master_fd, "M83")
        _write_line(master_fd, "ok")
        _write_line(master_fd, "G1 X10 Y10 E1.2 F1200")
        _write_line(master_fd, "ok")

        deadline = time.time() + 5.0
        while time.time() < deadline and not pause_seen:
            data = _read_available(master_fd, 0.2)
            if b"M600" in data:
                pause_seen = True
                break
            time.sleep(0.05)

    finally:
        # Always terminate and collect logs without blocking indefinitely.
        try:
            proc.terminate()
            out, _ = proc.communicate(timeout=2)
        except Exception:
            try:
                proc.kill()
                out, _ = proc.communicate(timeout=2)
            except Exception:
                out = out or ""

        for fd in (master_fd, slave_fd):
            try:
                os.close(fd)
            except Exception:
                pass

    assert pause_seen, f"Expected pause gcode (M600) not sent. Monitor output:\n{out}"
