

def test_notifier_never_raises(monkeypatch):
    from filmon.notify import Notifier

    def boom(*a, **k):
        raise RuntimeError("fail")

    monkeypatch.setattr("requests.post", boom)

    n = Notifier(enabled=True, pushover_token="t", pushover_user="u")

    # Call the sync path to deterministically exercise exception handling.
    n._send_sync("t", "m", 1)
