"""Push alerts for strong matches.

Two backends, use either or both:
- Home Assistant: the companion app's notify service (HA URL + service in Settings, HA_TOKEN in env).
- Apprise: ntfy, Discord, Telegram, Slack, email, Pushover and 100+ more. Service URLs carry
  tokens, so they live in the environment: APPRISE_URLS="ntfy://ntfy.sh/my-topic tgram://bot/chat".

Alerts go out as they're found ("instant", outside quiet hours) or once a day ("digest").
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta

import httpx
from sqlmodel import select

from .config import AppSettings, env, get_kv, get_settings, set_kv
from .credentials import redact
from .db import Job, session, utc_today, utcnow

log = logging.getLogger("jobscout.notify")


def apprise_urls() -> list[str]:
    return [u for u in re.split(r"[\s,]+", env("APPRISE_URLS")) if u]


def ha_ready(settings: AppSettings) -> bool:
    a = settings.alerts
    return bool(a.ha_enabled and a.ha_url and a.notify_service and env("HA_TOKEN"))


def apprise_ready(settings: AppSettings) -> bool:
    return bool(settings.alerts.apprise_enabled and apprise_urls())


def configured(settings: AppSettings) -> bool:
    return settings.alerts.enabled and (ha_ready(settings) or apprise_ready(settings))


def _quiet(settings: AppSettings, now: datetime | None = None) -> bool:
    a = settings.alerts
    try:
        start, end = time.fromisoformat(a.quiet_start), time.fromisoformat(a.quiet_end)
    except ValueError:
        return False
    t = (now or datetime.now()).time()
    return (start <= t or t < end) if start > end else (start <= t < end)


def _send_ha(settings: AppSettings, title: str, message: str, url: str) -> None:
    a = settings.alerts
    r = httpx.post(
        f"{a.ha_url.rstrip('/')}/api/services/notify/{a.notify_service}",
        headers={"Authorization": f"Bearer {env('HA_TOKEN')}"},
        json={"title": title, "message": message,
              "data": {"url": url, "clickAction": url, "group": "job-scout", "tag": "job-scout"}},
        timeout=15,
    )
    r.raise_for_status()


def _send_apprise(settings: AppSettings, title: str, message: str, url: str) -> None:
    import apprise  # imported lazily: it loads 100+ service plugins

    ap = apprise.Apprise()
    for u in apprise_urls():
        if not ap.add(u):
            raise RuntimeError("an APPRISE_URLS entry isn't a valid Apprise URL")
    result = ap.notify(title=title, body=f"{message}\n{url}")
    if not result:
        raise RuntimeError(f"{result.failed_count} of {len(result)} Apprise services failed")


def send(settings: AppSettings, title: str, message: str, url: str) -> dict[str, str]:
    """Send through every ready backend. Returns {backend: error message or ""}."""
    outcomes = {}
    for name, ready, fn in (("Home Assistant", ha_ready, _send_ha), ("Apprise", apprise_ready, _send_apprise)):
        if not ready(settings):
            continue
        try:
            fn(settings, title, message, url)
            outcomes[name] = ""
        except Exception as e:  # one broken backend mustn't stop the others
            outcomes[name] = redact(str(e)) or type(e).__name__
            log.warning("%s notify failed: %s", name, outcomes[name])
    return outcomes


def _digest_due(settings: AppSettings, now: datetime) -> bool:
    try:
        at = time.fromisoformat(settings.alerts.digest_time)
    except ValueError:
        at = time(18, 0)
    return now.time() >= at and get_kv("last_digest") != utc_today()


def flush_alerts() -> int:
    """Send unsent strong matches from the last 3 days - now (instant) or once a day (digest)."""
    settings = get_settings()
    if not configured(settings):
        return 0
    digest = settings.alerts.mode == "digest"
    now = datetime.now()
    if (digest and not _digest_due(settings, now)) or (not digest and _quiet(settings, now)):
        return 0
    base = settings.alerts.public_url.rstrip("/")
    with session() as s:
        jobs = s.exec(
            select(Job).where(
                Job.notified == False,  # noqa: E712
                Job.filtered_out == False,  # noqa: E712
                Job.score >= settings.alerts.threshold,
                Job.first_seen >= utcnow() - timedelta(days=3),
            ).order_by(Job.score.desc())
        ).all()
        if not jobs:
            if digest:
                set_kv("last_digest", utc_today())
            return 0
        if len(jobs) <= 3 and not digest:
            results = [send(settings, f"{j.score} · {j.title}", f"{j.company} · {j.location}\n{j.why}", f"{base}/?job={j.id}")
                       for j in jobs]
        else:
            top = "\n".join(f"{j.score} · {j.title} ({j.company})" for j in jobs[:8])
            label = "Today's" if digest else "New"
            results = [send(settings, f"{label} strong job matches: {len(jobs)}", top,
                            f"{base}/?min_score={settings.alerts.threshold}&sort=score")]
        if not any(err == "" for r in results for err in r.values()):
            return 0  # every backend failed - try again next time
        if digest:
            set_kv("last_digest", utc_today())
        for j in jobs:
            j.notified = True
            s.add(j)
        s.commit()
        return len(jobs)
