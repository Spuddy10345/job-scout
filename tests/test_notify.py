"""Alert fan-out across Home Assistant and Apprise."""

from datetime import datetime

import pytest

from app import notify
from app.config import get_settings, save_settings
from app.db import Job, session


@pytest.fixture
def both_backends(monkeypatch):
    monkeypatch.setenv("APPRISE_URLS", "ntfy://ntfy.sh/topic-one json://example.invalid/hook")
    monkeypatch.setenv("HA_TOKEN", "ha-token-123456")
    s = get_settings()
    s.alerts.ha_url, s.alerts.notify_service, s.alerts.threshold = "http://ha.local:8123", "mobile_app_x", 80
    s.alerts.quiet_start = s.alerts.quiet_end = "00:00"  # never quiet
    save_settings(s)
    sent = []
    monkeypatch.setattr(notify, "_send_ha", lambda st, t, m, u: sent.append(("ha", t)))
    monkeypatch.setattr(notify, "_send_apprise", lambda st, t, m, u: sent.append(("apprise", t)))
    return sent


def strong_job(score=90):
    with session() as s:
        s.add(Job(fingerprint=f"f{score}", title="Graduate Developer", company="Acme", url=f"https://e.com/{score}",
                  score=score, why="Good fit."))
        s.commit()


def test_apprise_urls_split_on_spaces_and_commas(monkeypatch):
    monkeypatch.setenv("APPRISE_URLS", "ntfy://a/b, tgram://x/y\n mailto://u:p@host")
    assert notify.apprise_urls() == ["ntfy://a/b", "tgram://x/y", "mailto://u:p@host"]


def test_instant_alert_goes_to_every_backend(both_backends):
    strong_job()
    assert notify.flush_alerts() == 1
    assert {b for b, _ in both_backends} == {"ha", "apprise"}
    assert notify.flush_alerts() == 0  # marked as notified


def test_one_failing_backend_does_not_block_the_other(both_backends, monkeypatch):
    def broken(*a):
        raise RuntimeError("401 for http://ha.local/api?token=ha-token-123456")

    monkeypatch.setattr(notify, "_send_ha", broken)
    results = notify.send(get_settings(), "t", "m", "u")
    assert results["Apprise"] == "" and "ha-token-123456" not in results["Home Assistant"]


def test_all_backends_failing_leaves_jobs_unsent(both_backends, monkeypatch):
    def broken(*a):
        raise RuntimeError("down")

    monkeypatch.setattr(notify, "_send_ha", broken)
    monkeypatch.setattr(notify, "_send_apprise", broken)
    strong_job()
    assert notify.flush_alerts() == 0
    monkeypatch.setattr(notify, "_send_apprise", lambda *a: None)
    assert notify.flush_alerts() == 1


def test_digest_waits_for_its_time_and_sends_once(both_backends, monkeypatch):
    s = get_settings()
    s.alerts.mode, s.alerts.digest_time = "digest", "18:00"
    save_settings(s)
    strong_job(91)
    strong_job(92)

    class Clock(datetime):
        hour = 17

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 28, cls.hour, 30)

    monkeypatch.setattr(notify, "datetime", Clock)
    assert notify.flush_alerts() == 0
    Clock.hour = 18
    assert notify.flush_alerts() == 2
    assert [t for _, t in both_backends if _ == "apprise"] == ["Today's strong job matches: 2"]
    strong_job(93)
    assert notify.flush_alerts() == 0  # already sent today


def test_nothing_configured_means_no_alerts(monkeypatch):
    monkeypatch.delenv("APPRISE_URLS", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert not notify.configured(get_settings())
