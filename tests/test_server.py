import json
import urllib.error
import urllib.request

import pytest

from grokbot import server

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url):
    try:
        with NO_PROXY.open(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture
def srv(tmp_path):
    s = server.start(tmp_path, port=0, host="127.0.0.1")
    yield s, tmp_path, f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def test_routes(srv):
    s, d, base = srv
    code, body = get(base + "/")
    assert code == 200 and "startet" in body                    # before the first write
    (d / "dashboard.html").write_text("<h1>Grok Bot</h1>")
    (d / "night.log.jsonl").write_text('{"kind": "ENTER"}\n')
    assert get(base + "/") == (200, "<h1>Grok Bot</h1>")
    assert get(base + "/log")[1].startswith('{"kind"')
    assert get(base + "/recap")[0] == 404
    (d / "recap.json").write_text(json.dumps({"end_balance": 80}))
    assert json.loads(get(base + "/recap")[1]) == {"end_balance": 80}


def test_only_run_folder_is_served(srv):
    s, d, base = srv
    (d / "secret.txt").write_text("x")
    for path in ("/secret.txt", "/../../etc/passwd", "/runs", "/%2e%2e/etc/passwd"):
        assert get(base + path)[0] == 404


def test_urls():
    s = server.start(__import__("pathlib").Path("."), port=0, host="0.0.0.0")
    try:
        u = server.urls(s)
        assert u[0].startswith("http://") and u[1].startswith("http://localhost:")
    finally:
        s.shutdown()
