#!/usr/bin/env python3
"""Local control client for filament-monitor.

The monitor holds the printer serial port, so external consoles cannot safely
share the device. filmonctl talks to the monitor over a local UNIX socket.

Commands:
  status | rearm | reset | enable | arm | unarm | disable

Socket path:
  - default: /run/filmon.sock
  - override: --socket PATH or FILMON_SOCKET env var
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

DEFAULT_SOCK = "/run/filmon/filmon.sock"


def _send(sock_path: str, cmd: str) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock_path)
        s.sendall((cmd.strip() + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data and len(data) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
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
    ap = argparse.ArgumentParser(description="Control filament-monitor via its local UNIX socket")
    ap.add_argument("command", choices=["status", "rearm", "reset", "enable", "arm", "unarm", "disable", "test-notify"],
                    help="Command to send to the daemon")
    ap.add_argument("--socket", default=os.environ.get("FILMON_SOCKET", DEFAULT_SOCK),
                    help=f"Control socket path (default: {DEFAULT_SOCK})")
    ap.add_argument("--json", action="store_true", help="Print raw JSON response")
    args = ap.parse_args()

    
    if args.command == "test-notify":
        import os, urllib.request, urllib.parse
        token = os.getenv("PUSHOVER_TOKEN")
        user = os.getenv("PUSHOVER_USER")
        if not token or not user:
            print("error: PUSHOVER_TOKEN and PUSHOVER_USER must be set", file=sys.stderr)
            return 2
        data = urllib.parse.urlencode({
            "token": token,
            "user": user,
            "title": "Filament Monitor",
            "message": "Test notification from filmonctl",
        }).encode()
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

    resp = _send(args.socket, args.command)
    if args.json:
        print(json.dumps(resp, indent=2, sort_keys=True))
    else:
        if resp.get("ok"):
            if args.command in ("status",):
                state = resp.get("state", {})
                ver = resp.get("version", "")
                print(f"ok  version={ver} enabled={state.get('enabled')} armed={state.get('armed')} latched={state.get('latched')} pulses_reset={state.get('motion_pulses_since_reset')}")
            else:
                print("ok")
        else:
            print(f"error: {resp.get('error','unknown error')}", file=sys.stderr)
            raw = resp.get("raw")
            if raw:
                print(raw, file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
