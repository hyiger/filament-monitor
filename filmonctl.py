#!/usr/bin/env python3
"""
Local control client for filament-monitor.

The monitor holds the printer serial port, so external consoles cannot safely
share the device. filmonctl talks to the monitor over a local UNIX socket.

Commands:
  status | rearm | reset | enable | arm | unarm | disable | test-notify | test-notify-local

test-notify goes through the daemon so the daemon's own Notifier (its
environment, its FILMON_NOTIFY gate) sends the test. test-notify-local POSTs
directly from this client's environment and proves nothing about the daemon.

Socket path:
  - default: /run/filmon/filmon.sock
  - override: --socket PATH or FILMON_SOCKET env var
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.request
import urllib.parse

DEFAULT_SOCK = "/run/filmon/filmon.sock"


def _send(sock_path: str, cmd: str, timeout_s: float = 5.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        # A missing/refusing/wedged daemon should yield a clean error, not a
        # traceback or a client hung forever in recv().
        try:
            s.connect(sock_path)
            s.sendall((cmd.strip() + "\n").encode("utf-8"))
            data = b""
            while b"\n" not in data and len(data) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as e:
            return {"ok": False, "error": f"cannot reach daemon at {sock_path}: {e}"}
        line = data.decode("utf-8", errors="replace").strip()
        if not line:
            return {"ok": False, "error": "empty response"}
        try:
            return json.loads(line)
        except Exception:
            return {"ok": False, "error": "non-json response", "raw": line}
    finally:
        try:
            s.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Control filament-monitor via its local UNIX socket"
    )
    ap.add_argument(
        "command",
        choices=[
            "status",
            "rearm",
            "reset",
            "enable",
            "arm",
            "unarm",
            "disable",
            "test-notify",
            "test-notify-local",
        ],
        help="Command to send to the daemon (test-notify-local POSTs from this client instead)",
    )
    ap.add_argument(
        "--socket",
        default=os.environ.get("FILMON_SOCKET", DEFAULT_SOCK),
        help=f"Control socket path (default: {DEFAULT_SOCK})",
    )
    ap.add_argument("--json", action="store_true", help="Print raw JSON response")
    args = ap.parse_args()

    # ------------------------------------------------------------
    # test-notify-local: local Pushover test (no daemon involvement)
    #
    # NOTE: this tests the CLIENT environment (this shell's PUSHOVER_* vars,
    # ignoring FILMON_NOTIFY) — it proves nothing about whether the daemon's
    # own alerts will deliver. Use plain 'test-notify' for that.
    # ------------------------------------------------------------
    if args.command == "test-notify-local":
        token = os.getenv("PUSHOVER_TOKEN")
        user = os.getenv("PUSHOVER_USER")

        if not token or not user:
            print(
                "error: PUSHOVER_TOKEN and PUSHOVER_USER must be set",
                file=sys.stderr,
            )
            return 2

        data = urllib.parse.urlencode(
            {
                "token": token,
                "user": user,
                "title": "Filament Monitor",
                "message": "Test notification from filmonctl",
            }
        ).encode()

        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    "https://api.pushover.net/1/messages.json",
                    data=data,
                    method="POST",
                ),
                timeout=5,
            )
            print("ok")
            return 0
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    # ------------------------------------------------------------
    # All other commands go to the daemon
    # ------------------------------------------------------------
    # test-notify blocks in the daemon until the HTTP outcome is known
    # (up to ~5 s), so give it a wider deadline than the instant commands.
    timeout_s = 15.0 if args.command == "test-notify" else 5.0
    resp = _send(args.socket, args.command, timeout_s=timeout_s)

    if args.json:
        print(json.dumps(resp, indent=2, sort_keys=True))
    else:
        if resp.get("ok"):
            if args.command == "status":
                state = resp.get("state", {})
                ver = resp.get("version", "")
                mode = state.get("mode")
                print(
                    f"ok  "
                    f"version={ver} "
                    f"mode={mode} "
                    f"armed={mode == 'armed'} "
                    f"latched={state.get('latched')} "
                    f"pulses_reset={state.get('motion_pulses_since_reset')}"
                )
            else:
                print("ok")
        else:
            print(f"error: {resp.get('error', 'unknown error')}", file=sys.stderr)
            raw = resp.get("raw")
            if raw:
                print(raw, file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
