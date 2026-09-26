"""In-process scheduler: one interval job per enabled source, plus alert flushing."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from . import notify, pipeline
from .config import get_settings, missing_keys
from .db import SourceRun, session
from .sources import SOURCES

log = logging.getLogger("jobscout.scheduler")

scheduler = BackgroundScheduler(
    executors={"default": ThreadPoolExecutor(4)},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
)


def _last_run(name: str) -> datetime | None:
    from sqlmodel import select

    with session() as s:
        return s.exec(
            select(SourceRun.started_at).where(SourceRun.source == name).order_by(SourceRun.started_at.desc())
        ).first()


def sync_jobs() -> None:
    """(Re)create source jobs from current settings. Called at startup and after settings change."""
    settings = get_settings()
    for job in scheduler.get_jobs():
        if job.id.startswith("source:"):
            job.remove()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i, (name, cfg) in enumerate(settings.sources.items()):
        if name not in SOURCES or not cfg.enabled or missing_keys(name):
            continue
        interval = timedelta(hours=max(cfg.interval_hours, 0.25))
        last = _last_run(name)
        # resume the cadence across restarts; stagger first runs so sources don't all fire at once
        first = max(last + interval, now + timedelta(seconds=20 + i * 45)) if last else now + timedelta(seconds=20 + i * 45)
        scheduler.add_job(
            pipeline.run_source, "interval", args=[name], id=f"source:{name}", seconds=interval.total_seconds(),
            next_run_time=first.replace(tzinfo=timezone.utc), replace_existing=True,  # DB times are naive UTC
        )
    log.info("scheduled: %s", [j.id for j in scheduler.get_jobs()])


def start() -> None:
    scheduler.add_job(notify.flush_alerts, "interval", minutes=15, id="alerts", replace_existing=True)
    scheduler.add_job(pipeline.score_pending, "interval", minutes=30, id="score", replace_existing=True)
    sync_jobs()
    scheduler.start()


def run_now(name: str) -> None:
    scheduler.add_job(pipeline.run_source, args=[name], id=f"manual:{name}", replace_existing=True)


def run_in_background(fn, *args) -> None:
    scheduler.add_job(fn, args=list(args), id=f"task:{fn.__name__}", replace_existing=True)


def next_runs() -> dict[str, datetime | None]:
    return {j.id.removeprefix("source:"): j.next_run_time for j in scheduler.get_jobs() if j.id.startswith("source:")}
