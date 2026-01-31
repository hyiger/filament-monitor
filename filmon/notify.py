from __future__ import annotations
import threading
from typing import Optional
import requests

class Notifier:
    def __init__(self, enabled: bool, pushover_token: Optional[str], pushover_user: Optional[str], timeout_s: float = 5.0):
        self.enabled = enabled and bool(pushover_token and pushover_user)
        self._token = pushover_token
        self._user = pushover_user
        self._timeout = timeout_s

    def send(self, title: str, message: str, priority: int = 0):
        if not self.enabled:
            return
        threading.Thread(target=self._send_sync, args=(title, message, priority), daemon=True).start()

    def _send_sync(self, title: str, message: str, priority: int):
        try:
            requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": self._token,
                    "user": self._user,
                    "title": title,
                    "message": message,
                    "priority": priority,
                },
                timeout=self._timeout,
            )
        except Exception:
            pass
