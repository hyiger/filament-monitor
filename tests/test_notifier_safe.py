
def test_notifier_never_raises(monkeypatch):
    from filmon.notify import Notifier
    def boom(*a, **k):
        raise RuntimeError("fail")
    monkeypatch.setattr("requests.post", boom)
    n = Notifier()
    # should not raise
    n.send("t","m",1)
