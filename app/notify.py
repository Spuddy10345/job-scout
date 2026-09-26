"""Home Assistant push alerts for strong matches (via the companion app's notify service)."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

import httpx
from sqlmodel import select

from .config import AppSettings, env, get_settings
from .db import Job, session, utcnow

log = logging.getLogger("jobscout.notify")


def configured(settings: AppSettings) -> bool:
    a = settings.alerts
    return bool(a.enabled and a.ha_url and a.notify_service and env("HA_TOKEN"))


def _quiet(settings: AppSettings, now: datetime | None = None) -> bool:
    a = settings.alerts
    try:
        start, end = time.fromisoformat(a.quiet_start), time.fromisoformat(a.quiet_end)
    except ValueError:
        return False
    t = (now or datetime.now()).time()
    return (start <= t or t < end) if start > end else (start <= t < end)


def send(settings: AppSettings, title: str, message: str, url: str) -> None:
    a = settings.alerts
    r = httpx.post(
        f"{a.ha_url.rstrip('/')}/api/services/notify/{a.notify_service}",
        headers={"Authorization": f"Bearer {env('HA_TOKEN')}"},
        json={"title": title, "message": message,
              "data": {"url": url, "clickAction": url, "group": "job-scout", "tag": "job-scout"}},
        timeout=15,
    )
    r.raise_for_status()


def flush_alerts() -> int:
    """Send any unsent strong matches from the last 3 days, unless in quiet hours."""
    settings = get_settings()
    if not configured(settings) or _quiet(settings):
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
            return 0
        try:
            if len(jobs) <= 3:
                for j in jobs:
                    send(settings, f"{j.score} · {j.title}", f"{j.company} · {j.location}\n{j.why}", f"{base}/?job={j.id}")
            else:
                top = "\n".join(f"{j.score} · {j.title} ({j.company})" for j in jobs[:5])
                send(settings, f"{len(jobs)} new strong job matches", top, f"{base}/?min_score={settings.alerts.threshold}&sort=score")
        except httpx.HTTPError as e:
            log.warning("HA notify failed: %s", e)
            return 0
        for j in jobs:
            j.notified = True
            s.add(j)
        s.commit()
        return len(jobs)
