"""Everything around the core job list: paging, bulk actions, CSV export, saved views, first-run
setup, integration status and the stats page."""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import col, select

from .. import llm, pages, pipeline, scheduler
from ..config import get_kv, get_settings, missing_keys, save_settings, set_kv, settings_saved
from ..credentials import env
from ..db import STATUSES, Job, JobEvent, session, utcnow
from ..sources import SOURCES
from .routes import _int, _query_jobs, filter_params, render, table_response, templates

router = APIRouter()

# name, env vars, what it's for, where to get it
INTEGRATIONS = [
    ("Anthropic (Claude)", ["ANTHROPIC_API_KEY"], "Scores jobs, reads careers pages, drafts application notes",
     "https://console.anthropic.com/settings/keys"),
    ("Adzuna", ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"], "Job search API covering many boards - free key",
     "https://developer.adzuna.com/signup"),
    ("Reed", ["REED_API_KEY"], "UK job search API - free key", "https://www.reed.co.uk/developers/jobseeker"),
    ("Brave Search", ["BRAVE_API_KEY"], "Finds postings on company sites; company lookups - $5/month free credit",
     "https://api-dashboard.search.brave.com"),
    ("Firecrawl", ["FIRECRAWL_API_KEY"], "Reads boards without an API and JavaScript careers pages - 1,000 free credits/month",
     "https://www.firecrawl.dev"),
    ("Apprise", ["APPRISE_URLS"], "Push alerts to ntfy, Discord, Telegram, email and 100+ more",
     "https://github.com/caronc/apprise/wiki"),
    ("Home Assistant", ["HA_TOKEN"], "Push alerts through the companion app", ""),
    ("Web UI password", ["JOBSCOUT_PASSWORD"], "Requires a login - set this if anyone else can reach the app", ""),
]


def integrations() -> list[dict]:
    return [{"name": name, "envs": keys, "purpose": purpose, "url": url, "ready": all(env(k) for k in keys)}
            for name, keys, purpose, url in INTEGRATIONS]


templates.env.globals["integrations"] = integrations


# ---------------------------------------------------------------- job list: paging, bulk, export

@router.get("/jobs/rows", response_class=HTMLResponse)
def job_rows(request: Request):
    p = filter_params(request.query_params)
    offset = _int(request.query_params.get("offset"))
    rows, total = _query_jobs(p, offset)
    return templates.TemplateResponse(request, "_rows_page.html", {
        "jobs": rows, "total": total, "p": p, "next_offset": offset + len(rows), "oob": True,
        "since": request.cookies.get("since"),
    })


@router.post("/jobs/bulk", response_class=HTMLResponse)
async def bulk(request: Request):
    form = await request.form()
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    action = form.get("action", "")
    if ids and (action in STATUSES or action == "rescore"):
        with session() as s:
            for job in s.exec(select(Job).where(col(Job.id).in_(ids))).all():
                if action == "rescore":
                    job.score, job.score_attempts = None, 0
                elif job.status != action:
                    s.add(JobEvent(job_id=job.id, kind="status", text=f"{job.status} → {action}"))
                    job.status = action
                s.add(job)
            s.commit()
        if action == "rescore":
            scheduler.run_in_background(pipeline.score_pending)
    p = filter_params({k: v for k, v in form.items() if k not in ("ids", "action")})
    return table_response(request, p)


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value) -> str:
    """Scraped text ends up in spreadsheets: neutralise anything a spreadsheet would run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _csv(filename: str, header: list[str], rows) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow([_cell(v) for v in row])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _sources(job: Job) -> str:
    return ", ".join(sorted({x["source"] for x in job.sources}))


@router.get("/export/jobs.csv")
def export_jobs(request: Request):
    rows, _ = _query_jobs(filter_params(request.query_params), limit=5000)
    return _csv("jobs.csv", [
        "id", "score", "title", "company", "location", "distance_mi", "remote", "salary_min", "salary_max",
        "category", "status", "posted_at", "first_seen", "sources", "url", "why",
    ], ([j.id, j.score, j.title, j.company, j.location, j.distance_mi, j.remote, j.salary_min, j.salary_max,
         j.category, j.status, j.posted_at, j.first_seen, _sources(j), j.apply_url or j.url, j.why] for j in rows))


@router.get("/export/tracker.csv")
def export_tracker():
    with session() as s:
        jobs = s.exec(select(Job).where(col(Job.status).in_(["interested", "applied", "interview", "offer", "rejected"]))
                      .order_by(Job.status, Job.score.desc())).all()
        events = s.exec(select(JobEvent).order_by(JobEvent.at)).all()
    history: dict[int, list[str]] = {}
    for e in events:
        history.setdefault(e.job_id, []).append(f"{e.at:%Y-%m-%d} {e.text}")
    return _csv("applications.csv", ["id", "status", "score", "title", "company", "location", "url", "history"],
                ([j.id, j.status, j.score, j.title, j.company, j.location, j.apply_url or j.url,
                  " | ".join(history.get(j.id, []))] for j in jobs))


# ---------------------------------------------------------------- saved views

def _views_fragment(request: Request, current: str = "") -> HTMLResponse:
    return templates.TemplateResponse(request, "_views.html", {"views": get_kv("saved_views", {}), "current": current})


@router.post("/views", response_class=HTMLResponse)
def save_view(request: Request, name: str = Form(...), query: str = Form("")):
    name = re.sub(r"\s+", " ", name).strip()[:40]
    query = query.lstrip("?")
    views = get_kv("saved_views", {})
    if name:
        views[name] = query
        set_kv("saved_views", dict(sorted(views.items())))
    return _views_fragment(request, query)


@router.post("/views/delete", response_class=HTMLResponse)
def delete_view(request: Request, name: str = Form(...)):
    views = get_kv("saved_views", {})
    views.pop(name, None)
    set_kv("saved_views", views)
    return _views_fragment(request)


# ---------------------------------------------------------------- first-run setup

@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request):
    return render(request, "setup.html", s=get_settings(), missing={n: missing_keys(n) for n in SOURCES})


@router.post("/setup/done")
def setup_done():
    if not settings_saved():
        save_settings(get_settings())
    set_kv("setup_done", True)
    scheduler.sync_jobs()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- sources

@router.post("/runs", response_class=HTMLResponse)
def run_all(request: Request):
    from .routes import _runs_ctx

    settings = get_settings()
    for name, cfg in settings.sources.items():
        if name in SOURCES and cfg.enabled and not missing_keys(name):
            scheduler.run_now(name)
    return templates.TemplateResponse(request, "_runs.html", _runs_ctx())


# ---------------------------------------------------------------- stats

def _bars(items: list[tuple[str, float]], fmt: str = "{:.0f}") -> list[dict]:
    top = max((v for _, v in items), default=0) or 1
    return [{"label": label, "value": v, "text": fmt.format(v), "pct": round(100 * v / top, 1)} for label, v in items]


STAGES = ["interested", "applied", "interview", "offer"]


def _stats() -> dict:
    now = utcnow()
    since = now - timedelta(weeks=8)
    with session() as s:
        recent = s.exec(select(Job.first_seen, Job.sources).where(Job.first_seen >= since)).all()
        scores = s.exec(select(Job.score).where(Job.score != None, Job.filtered_out == False)).all()  # noqa: E711,E712
        statuses = dict(s.exec(select(Job.id, Job.status).where(Job.status != "new")).all())
        moves = s.exec(select(JobEvent.job_id, JobEvent.text).where(JobEvent.kind == "status")).all()

    # jobs found per week (oldest first) and by the source that found them first
    weeks = Counter(min(7, (now - seen).days // 7) for seen, _ in recent)
    per_week = [(f"{(now - timedelta(weeks=w)):%d %b}", weeks.get(w, 0)) for w in range(7, -1, -1)]
    labels = {n: src.label for n, src in SOURCES.items()}
    by_source = Counter(labels.get(src[0]["source"], src[0]["source"]) for _, src in recent if src)

    # score histogram in bands of ten
    bands = Counter(min(int(x) // 10, 9) for x in scores)
    histogram = [(f"{b * 10}–{b * 10 + 9 if b < 9 else 100}", bands.get(b, 0)) for b in range(10)]

    # furthest stage each job ever reached, from its current status and status history
    reached: dict[int, int] = {}
    for job_id, status in statuses.items():
        if status in STAGES:
            reached[job_id] = STAGES.index(status)
    for job_id, text in moves:
        for i, stage in enumerate(STAGES):
            if text.endswith(f"→ {stage}"):
                reached[job_id] = max(reached.get(job_id, -1), i)
    funnel = [(stage.capitalize(), sum(1 for r in reached.values() if r >= i)) for i, stage in enumerate(STAGES)]

    usage = llm.usage_by_day()
    fc = pages.credits_by_day()
    days = [(now - timedelta(days=d)).date().isoformat() for d in range(29, -1, -1)]
    return {
        "per_week": _bars(per_week), "by_source": _bars(by_source.most_common()),
        "histogram": _bars(histogram), "funnel": _bars(funnel),
        "spend": _bars([(d[5:], usage.get(d, {}).get("usd", 0.0)) for d in days], "${:.3f}"),
        "credits": _bars([(d[5:], fc.get(d, 0)) for d in days]),
        "totals": {
            "found_8w": len(recent), "scored": len(scores), "applied": funnel[1][1],
            "spend_30d": sum(usage.get(d, {}).get("usd", 0.0) for d in days),
            "credits_30d": sum(fc.get(d, 0) for d in days),
        },
    }


@router.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    return render(request, "stats.html", st=_stats())
