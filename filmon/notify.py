from __future__ import annotations
import threading
import time
from typing import Optional
import requests

class Notifier:
    """Sends best-effort push notifications via Pushover.

    Enabled only when FILMON_NOTIFY=1 and both PUSHOVER_TOKEN and
    PUSHOVER_USER environment variables are set. Each notification is
    dispatched on a short-lived daemon thread so it never blocks the
    main monitoring loop.

    Delivery outcomes are logged (notify_sent / notify_failed) when a logger
    is provided. Priority >= 1 messages (jam/runout alerts) are retried up to
    RETRY_ATTEMPTS extra times with RETRY_BACKOFF_S between attempts.
    """

    RETRY_ATTEMPTS = 2      # extra attempts for priority >= 1 messages
    RETRY_BACKOFF_S = 2.0   # delay between attempts (short-lived daemon thread; blocking sleep is fine)

    def __init__(self, enabled: bool, pushover_token: Optional[str], pushover_user: Optional[str], timeout_s: float = 5.0, logger=None):
        self.enabled = enabled and bool(pushover_token and pushover_user)
        self._token = pushover_token
        self._user = pushover_user
        self._timeout = timeout_s
        self._logger = logger

    def _emit(self, event: str, **fields):
        """Best-effort log emit. The notifier must never raise."""
        if self._logger is None:
            return
        try:
            self._logger.emit(event, **fields)
        except Exception:
            pass

    def send(self, title: str, message: str, priority: int = 0):
        """Queue a notification for background delivery. No-op when disabled."""
        if not self.enabled:
            return
        threading.Thread(target=self._send_sync, args=(title, message, priority), daemon=True).start()

    def send_sync(self, title: str, message: str, priority: int = 0) -> bool:
        """Blocking send that reports the actual delivery outcome.

        Used by the daemon's test-notify command: a fire-and-forget send()
        would report success before the HTTP result is known, making the test
        pass exactly when credentials or connectivity are broken. No-op
        (False) when disabled.
        """
        if not self.enabled:
            return False
        return self._send_sync(title, message, priority)

    def _send_sync(self, title: str, message: str, priority: int) -> bool:
        """Blocking HTTP POST to Pushover. Never raises (best-effort).

        Returns True when a delivery attempt succeeded.

        Checks the HTTP status code and Pushover's JSON 'status' field (1 on
        success, when parseable), emitting notify_sent / notify_failed. Priority
        >= 1 messages get RETRY_ATTEMPTS extra attempts with RETRY_BACKOFF_S
        backoff so transient failures do not drop jam/runout alerts silently.
        """
        attempts = 1 + (self.RETRY_ATTEMPTS if priority >= 1 else 0)
        for attempt in range(1, attempts + 1):
            status_code = None
            error = None
            try:
                resp = requests.post(
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
                status_code = resp.status_code
                ok = 200 <= status_code < 300
                if ok:
                    # Pushover reports success as JSON {"status": 1}; consult it when parseable.
                    try:
                        body = resp.json()
                        if isinstance(body, dict) and body.get("status") != 1:
                            ok = False
                            error = "pushover status != 1"
                    except Exception:
                        pass
                if ok:
                    self._emit("notify_sent", title=title, priority=priority, attempt=attempt)
                    return True
                if error is None:
                    error = f"http {status_code}"
            except Exception as e:
                error = str(e)
            self._emit("notify_failed", title=title, priority=priority, attempt=attempt, status=status_code, error=error)
            if attempt < attempts:
                time.sleep(self.RETRY_BACKOFF_S)
        return False
