
def test_requests_in_requirements():
    with open("requirements.txt") as f:
        txt = f.read().lower()
    assert "requests" in txt
