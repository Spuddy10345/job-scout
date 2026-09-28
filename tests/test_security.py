"""Secret handling, CSRF, login and output sanitising."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import credentials
from app.db import DATA_DIR
from app.main import app
from app.sources.adzuna import Adzuna
from app.web.routes import safe_url


@pytest.fixture
def client():
    return TestClient(app)  # no context manager: skips the lifespan, so the scheduler stays off


def test_redact_masks_env_values_and_query_keys(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "BSAsupersecretvalue")
    text = ("Client error for url 'https://api.adzuna.com/v1/api/jobs/gb/search/1?app_id=abc&app_key=zzz"
            "&what=x' and header BSAsupersecretvalue, Authorization: Bearer abcdefghijklmnop123")
    out = credentials.redact(text)
    assert "BSAsupersecretvalue" not in out and "app_key=***" in out and "app_id=***" in out
    assert "what=x" in out and "abcdefghijklmnop123" not in out


def test_secret_from_file(monkeypatch, tmp_path):
    secret = tmp_path / "reed"
    secret.write_text("file-secret\n")
    monkeypatch.delenv("REED_API_KEY", raising=False)
    monkeypatch.setenv("REED_API_KEY_FILE", str(secret))
    assert credentials.env("REED_API_KEY") == "file-secret"


def test_adzuna_errors_never_include_the_key(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id123456")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key7654321")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorised", request=request)

    real_client = httpx.Client
    monkeypatch.setattr("app.net.httpx.Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    from app.config import get_settings

    with pytest.raises(RuntimeError) as err:
        Adzuna().fetch(get_settings())
    assert "key7654321" not in str(err.value) and "401" in str(err.value)


@pytest.mark.parametrize("url,ok", [
    ("https://example.com/job/1", True), ("http://example.com", True),
    ("javascript:alert(1)", False), ("JaVaScRiPt:alert(1)", False), ("data:text/html,x", False), ("", False),
])
def test_safe_url(url, ok):
    assert bool(safe_url(url)) is ok


def test_cross_site_post_is_blocked(client):
    r = client.post("/settings/alerts", data={"ha_url": "https://evil.example"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/settings/alerts", data={"ha_url": "https://evil.example"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_same_origin_post_is_allowed(client):
    r = client.post("/settings/alerts", data={"ha_url": "http://ha.local:8123"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and "saved" in r.text


def test_security_headers(client):
    r = client.get("/healthz")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"


def test_login_required_when_password_set(client, monkeypatch):
    monkeypatch.setenv("JOBSCOUT_PASSWORD", "hunter2hunter2")
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/job/1", headers={"HX-Request": "true"}).headers["hx-redirect"].startswith("/login")
    assert client.get("/healthz").status_code == 200
    assert client.post("/login", data={"password": "wrong"}).status_code == 401
    r = client.post("/login", data={"password": "hunter2hunter2", "next": "//evil.example"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/settings").status_code == 200


def test_no_login_without_password(client, monkeypatch):
    monkeypatch.delenv("JOBSCOUT_PASSWORD", raising=False)
    assert client.get("/settings").status_code == 200


def test_error_messages_are_escaped(client):
    r = client.post("/settings/watchlist", data={"watchlist": "<img src=x onerror=alert(1)>"})
    assert "<img" not in r.text and "&lt;img" in r.text


def test_cv_upload_rejects_bad_type_and_cleans_up(client):
    r = client.post("/settings/cv", files={"cv": ("cv.exe", b"MZ", "application/octet-stream")})
    assert "must be a PDF" in r.text
    client.post("/settings/cv", files={"cv": ("cv.md", b"# Me", "text/markdown")})
    client.post("/settings/cv", files={"cv": ("cv.txt", b"Me again", "text/plain")})
    assert sorted(p.name for p in DATA_DIR.glob("cv.*")) == ["cv.txt"]
    client.post("/settings/cv/delete")
    assert not list(DATA_DIR.glob("cv.*"))
