from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sqlmodel import col, func, or_, select

from .. import enrich, llm, notify, pages, pipeline, scheduler
from ..auth import is_local_host, password, password_ok, safe_next
from ..credentials import env, redact
from ..config import (AppSettings, Centre, SourceSettings, WatchEntry, cached_settings, categories, currency, get_kv,
                      get_settings, missing_keys, reset_settings, save_settings, set_kv, settings_saved)
from ..prompts import DEFAULT_PERSONA, DEFAULT_RUBRIC
from ..db import DATA_DIR, STATUSES, Job, JobEvent, SourceRun, session, utcnow
from ..profile import extract_cv_text, profile_text
from ..sources import SOURCES

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

SORTS = {
    "score": Job.score, "posted": Job.posted_at, "seen": Job.first_seen, "salary": Job.salary_max,
    "distance": Job.distance_mi, "company": Job.company, "title": Job.title,
}

# How each site expects you to apply - shown on every job alongside the model's advert-specific steps.
SOURCE_GUIDE = {
    "adzuna": "Adzuna links out to the original advertiser - check where it lands (agency, company ATS or another board) and apply there.",
    "reed": "Apply on Reed with your Reed profile. Upload your latest CV to Reed first; the cover letter box is optional but read by recruiters.",
    "jobs_ac_uk": "University portal. Expect an online form plus a supporting statement that answers each essential criterion in the person specification.",
    "findajob": "Find a Job usually forwards you to the employer's site or asks you to email a CV. Follow the 'Apply for this job' button.",
    "indeed": "Indeed Apply uses the CV on your Indeed profile - refresh it there first. Some adverts redirect to the company site instead.",
    "gumtree": "Reply through Gumtree's message form: a short intro saying why you fit, and attach your CV as a PDF.",
    "totaljobs": "Apply with your Totaljobs profile CV. Adverts are often from agencies - a phone call to the consultant helps.",
    "cwjobs": "Apply with your CWJobs profile CV. Mostly IT agencies - follow up by phone or email with the named consultant.",
    "linkedin": "Easy Apply uses your LinkedIn profile + uploaded CV. Also message the poster or a team member directly.",
    "nhs": "NHS Jobs application form with a supporting statement mapped to the person specification.",
    "brave": "Found on the open web - apply wherever the link lands (company site, Civil Service Jobs, etc.). Civil Service roles use Success Profiles behaviour statements.",
    "watchlist": "Straight from the employer's careers system - apply there directly, no middleman.",
}
METHOD_LABEL = {
    "easy-apply": "Easy apply", "email-cv": "Email your CV", "company-ats": "Company application site",
    "gumtree-message": "Gumtree message", "application-form": "Application form", "agency": "Via recruitment agency",
    "unknown": "Check the advert",
}


# ---------------------------------------------------------------- template helpers

def timeago(dt: datetime | None) -> str:
    if not dt:
        return ""
    secs = (utcnow() - dt).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    days = int(secs // 86400)
    return f"{days}d ago" if days < 60 else dt.strftime("%d %b %Y")


def score_class(score: int | None) -> str:
    if score is None:
        return "s-none"
    return "s-high" if score >= 75 else "s-mid" if score >= 50 else "s-low"


def local(dt: datetime | None) -> datetime | None:
    """DB times are naive UTC; show them in the server's local time zone."""
    return dt.replace(tzinfo=timezone.utc).astimezone() if dt else dt


def money(v: float | None) -> str:
    return f"{currency()}{v / 1000:.0f}k" if v else ""


def safe_url(url: str | None) -> str:
    """Only http(s) links from scraped or model-written data - never javascript: or data:."""
    url = (url or "").strip()
    return url if urlsplit(url).scheme.lower() in ("http", "https") else ""


templates.env.filters.update(local=local, timeago=timeago, score_class=score_class, money=money, safe_url=safe_url)
def cat_class(name: str | None) -> str:
    cats = categories(cached_settings())[:-1]  # "Not-relevant" stays grey
    return f"cat-c{cats.index(name) % 6}" if name in cats else ""


templates.env.globals.update(STATUSES=STATUSES, SOURCES=SOURCES, METHOD_LABEL=METHOD_LABEL,
                             categories=lambda: categories(cached_settings()), cat_class=cat_class)


def _counts() -> dict:
    with session() as s:
        total = s.exec(select(func.count()).select_from(Job).where(Job.filtered_out == False)).one()  # noqa: E712
        pending = s.exec(select(func.count()).select_from(Job).where(Job.score == None, Job.filtered_out == False)).one()  # noqa: E711,E712
        strong = s.exec(select(func.count()).select_from(Job).where(Job.score >= 75, Job.filtered_out == False, Job.status == "new")).one()  # noqa: E712
    settings = get_settings()
    return {
        "total": total, "pending": pending, "strong": strong,
        "llm_used": llm.usage_today(), "llm_limit": settings.llm.daily_limit,
        "cost_today": llm.cost_today(), "cost_month": llm.cost_month(), "batched": pipeline.batches_pending(),
        "fc_used": pages.credits_today(), "fc_limit": settings.llm.firecrawl_daily_credits,
        "llm_ok": llm.available(),
        "placeholders": settings.profile_mode != "cv" and "[" in settings.profile_md,
    }


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    open_access = not password() and not is_local_host(request)
    return templates.TemplateResponse(request, name, {"counts": _counts(), "open_access": open_access,
                                                       "auth_enabled": bool(password()), **ctx})


# ---------------------------------------------------------------- login

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if not password() or request.session.get("auth"):
        return RedirectResponse(safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": ""})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, password_: str = Form("", alias="password"), next: str = Form("/")):
    if password_ok(password_):
        request.session.clear()
        request.session["auth"] = True
        return RedirectResponse(safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": "Wrong password"},
                                      status_code=401)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- jobs

PAGE_SIZE = 100
NON_FILTER_PARAMS = ("job", "offset", "limit")


def filter_params(query_params) -> dict:
    """The job-list filters from a query string, without paging/drawer state or empty values."""
    return {k: v for k, v in query_params.items() if v not in ("", None) and k not in NON_FILTER_PARAMS}


def _job_filter(p: dict):
    stmt = select(Job)
    if p.get("filtered") == "only":
        stmt = stmt.where(Job.filtered_out == True)  # noqa: E712
    elif p.get("filtered") != "all":
        stmt = stmt.where(Job.filtered_out == False)  # noqa: E712
    status = p.get("status") or "active"
    if status == "active":
        stmt = stmt.where(col(Job.status).not_in(["hidden", "rejected"]))
    elif status != "any":
        stmt = stmt.where(Job.status == status)
    if q := (p.get("q") or "").strip():
        like = f"%{q}%"
        stmt = stmt.where(or_(col(Job.title).ilike(like), col(Job.company).ilike(like),
                              col(Job.location).ilike(like), col(Job.description).ilike(like)))
    if cat := p.get("category"):
        stmt = stmt.where(Job.category == cat)
    if src := p.get("source"):
        stmt = stmt.where(func.json_extract(Job.sources, "$").like(f'%"source":"{src}"%'))
    if (ms := p.get("min_score")) and str(ms).isdigit() and int(ms) > 0:
        stmt = stmt.where(Job.score >= int(ms))
    if p.get("remote") == "only":
        stmt = stmt.where(Job.remote == True)  # noqa: E712
    elif p.get("remote") == "exclude":
        stmt = stmt.where(Job.remote == False)  # noqa: E712
    if (days := p.get("days")) and str(days).isdigit():
        stmt = stmt.where(Job.first_seen >= utcnow() - timedelta(days=int(days)))

    sort = SORTS.get(p.get("sort") or "score", Job.score)
    desc = (p.get("dir") or ("asc" if p.get("sort") in ("distance", "company", "title") else "desc")) == "desc"
    return stmt, [sort.is_(None), sort.desc() if desc else sort.asc(), Job.first_seen.desc(), Job.id.desc()]


def _query_jobs(p: dict, offset: int = 0, limit: int = PAGE_SIZE) -> tuple[list[Job], int]:
    stmt, order = _job_filter(p)
    with session() as s:
        total = s.exec(select(func.count()).select_from(stmt.subquery())).one()
        rows = s.exec(stmt.order_by(*order).offset(max(offset, 0)).limit(min(max(limit, 1), 5000))).all()
    return list(rows), total


def _int(value, default: int = 0) -> int:
    return int(value) if str(value or "").isdigit() else default


@router.get("/", response_class=HTMLResponse)
def jobs_page(request: Request):
    if not settings_saved() and not get_kv("setup_done"):
        return RedirectResponse("/setup", status_code=303)
    prev = get_kv("last_visit")
    since = request.cookies.get("since") or prev
    # a visit more than 30 min after the last one starts a new "since" window
    if not prev or datetime.fromisoformat(prev) < utcnow() - timedelta(minutes=30):
        since = prev
    set_kv("last_visit", utcnow().isoformat())
    p = filter_params(request.query_params)
    rows, total = _query_jobs(p)
    resp = render(request, "jobs.html", jobs=rows, total=total, p=p, since=since, next_offset=len(rows),
                  open_job=request.query_params.get("job"), views=get_kv("saved_views", {}))
    if since:
        resp.set_cookie("since", since, max_age=1800)
    return resp


@router.get("/jobs/table", response_class=HTMLResponse)
def jobs_table(request: Request):
    return table_response(request, filter_params(request.query_params))


def table_response(request: Request, p: dict) -> HTMLResponse:
    rows, total = _query_jobs(p)
    resp = templates.TemplateResponse(request, "_table.html", {"jobs": rows, "total": total, "p": p, "next_offset": len(rows),
                                                                "since": request.cookies.get("since")})
    resp.headers["HX-Push-Url"] = "/?" + urlencode(p) if p else "/"
    return resp


def _job_ctx(job_id: int) -> dict:
    with session() as s:
        job = s.get(Job, job_id)
        events = s.exec(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.at.desc())).all()
    sources = sorted({x["source"] for x in job.sources}) if job else []
    return {"job": job, "events": events, "guides": [(SOURCES[x].label if x in SOURCES else x, SOURCE_GUIDE.get(x, ""))
                                                       for x in sources], "enrich_ok": enrich.available()}


@router.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    return templates.TemplateResponse(request, "_job_detail.html", _job_ctx(job_id))


@router.post("/job/{job_id}/status", response_class=HTMLResponse)
def job_status(request: Request, job_id: int, status: str = Form(...), view: str = Form("drawer")):
    with session() as s:
        job = s.get(Job, job_id)
        if job and status in STATUSES and job.status != status:
            s.add(JobEvent(job_id=job_id, kind="status", text=f"{job.status} → {status}"))
            job.status = status
            s.add(job)
            s.commit()
    if view == "board":
        return tracker_board(request)
    resp = templates.TemplateResponse(request, "_job_detail.html", _job_ctx(job_id))
    resp.headers["HX-Trigger"] = "jobs-changed"
    return resp


@router.post("/job/{job_id}/note", response_class=HTMLResponse)
def job_note(request: Request, job_id: int, text: str = Form(...)):
    if text.strip():
        with session() as s:
            s.add(JobEvent(job_id=job_id, kind="note", text=text.strip()))
            s.commit()
    return templates.TemplateResponse(request, "_job_detail.html", _job_ctx(job_id))


@router.post("/job/{job_id}/cover", response_class=HTMLResponse)
def job_cover(request: Request, job_id: int):
    settings = get_settings()
    with session() as s:
        job = s.get(Job, job_id)
        try:
            job.cover_note = llm.draft_cover_note(settings, profile_text(settings), pipeline._job_payload(job))
            s.add(job)
            s.commit()
            error = ""
        except Exception as e:
            error = redact(str(e))
    return templates.TemplateResponse(request, "_cover.html", {"job": job, "error": error})


@router.post("/job/{job_id}/rescore", response_class=HTMLResponse)
def job_rescore(request: Request, job_id: int, fetch: bool = Form(False)):
    settings = get_settings()
    error = ""
    with session() as s:
        job = s.get(Job, job_id)
        try:
            if fetch:  # pull the full advert text first - board snippets are short
                text, _ = pages.get_page_text(settings, job.url, min_chars=400)
                if len(text) > len(job.description):
                    job.description = text[:15000]
            pipeline.score_job(job, settings, profile_text(settings))
            s.add(job)
            s.commit()
        except Exception as e:
            error = redact(str(e))
    ctx = _job_ctx(job_id)
    ctx["error"] = error
    resp = templates.TemplateResponse(request, "_job_detail.html", ctx)
    resp.headers["HX-Trigger"] = "jobs-changed"
    return resp


@router.post("/job/{job_id}/enrich", response_class=HTMLResponse)
def job_enrich(request: Request, job_id: int):
    error = ""
    with session() as s:
        job = s.get(Job, job_id)
        try:
            enrich.enrich_company(job)
            s.add(job)
            s.commit()
        except Exception as e:
            error = redact(str(e))
    ctx = _job_ctx(job_id)
    ctx["error"] = error
    return templates.TemplateResponse(request, "_job_detail.html", ctx)


# ---------------------------------------------------------------- tracker

@router.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request):
    return render(request, "tracker.html", **_board_ctx())


def _board_ctx() -> dict:
    cols = ["interested", "applied", "interview", "offer", "rejected"]
    with session() as s:
        jobs = s.exec(select(Job).where(col(Job.status).in_(cols)).order_by(Job.score.desc())).all()
        latest = (select(JobEvent.job_id, func.max(JobEvent.id).label("id"))
                  .where(col(JobEvent.job_id).in_([j.id for j in jobs])).group_by(JobEvent.job_id).subquery())
        last = {e.job_id: e for e in s.exec(select(JobEvent).join(latest, JobEvent.id == latest.c.id)).all()}
    board = {c: [j for j in jobs if j.status == c] for c in cols}
    stale = {j.id for j in board["applied"] if j.id in last and last[j.id].at < utcnow() - timedelta(days=10)}
    return {"board": board, "last": last, "stale": stale}


def tracker_board(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_board.html", _board_ctx())


# ---------------------------------------------------------------- runs

def _runs_ctx() -> dict:
    settings = get_settings()
    nxt = scheduler.next_runs()
    rows = []
    with session() as s:
        for name, src in SOURCES.items():
            last = s.exec(select(SourceRun).where(SourceRun.source == name).order_by(SourceRun.started_at.desc())).first()
            cfg = settings.sources.get(name, SourceSettings(enabled=False))
            rows.append({"name": name, "src": src, "cfg": cfg, "last": last, "missing": missing_keys(name),
                         "next": nxt[name].astimezone() if nxt.get(name) else None})
        recent = s.exec(select(SourceRun).order_by(SourceRun.started_at.desc()).limit(60)).all()
    return {"rows": rows, "recent": recent}


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    return render(request, "runs.html", **_runs_ctx())


@router.get("/runs/table", response_class=HTMLResponse)
def runs_table(request: Request):
    return templates.TemplateResponse(request, "_runs.html", _runs_ctx())


@router.post("/runs/{name}", response_class=HTMLResponse)
def run_now(request: Request, name: str):
    if name in SOURCES:
        scheduler.run_now(name)
    return templates.TemplateResponse(request, "_runs.html", _runs_ctx())


# ---------------------------------------------------------------- settings

def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _saved(msg: str = "Saved") -> Response:
    return HTMLResponse(f'<span class="saved">✓ {escape(msg)}</span>')


def _err(msg: str) -> Response:
    return HTMLResponse(f'<span class="err">{escape(redact(msg))}</span>')


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    s = get_settings()
    return render(request, "settings.html", s=s, missing={n: missing_keys(n) for n in SOURCES}, models=llm.list_models(),
                  alerts_ok=notify.configured(s), ha_token=bool(env("HA_TOKEN")),
                  apprise_count=len(notify.apprise_urls()))


@router.post("/settings/profile", response_class=HTMLResponse)
def save_profile(profile_md: str = Form(""), profile_mode: str = Form("both")):
    s = get_settings()
    s.profile_md, s.profile_mode = profile_md, profile_mode if profile_mode in ("both", "profile", "cv") else "both"
    save_settings(s)
    return _saved("Profile saved - use Re-score all to apply it to existing jobs")


CV_TYPES = {".pdf", ".docx", ".md", ".markdown", ".txt"}
CV_MAX_BYTES = 10 * 1024 * 1024


def _remove_cv_files() -> None:
    for f in DATA_DIR.glob("cv.*"):
        f.unlink(missing_ok=True)


@router.post("/settings/cv", response_class=HTMLResponse)
async def upload_cv(request: Request, cv: UploadFile = File(...), next: str = Form("/settings#profile")):
    suffix = Path(cv.filename or "").suffix.lower()
    if suffix not in CV_TYPES:
        return _err("CV must be a PDF, DOCX, Markdown or text file")
    data = await cv.read(CV_MAX_BYTES + 1)
    if len(data) > CV_MAX_BYTES:
        return _err("CV is larger than 10 MB")
    try:
        text = extract_cv_text(cv.filename or "cv", data)
    except Exception as e:
        return _err(f"Could not read CV: {e}")
    _remove_cv_files()
    (DATA_DIR / f"cv{suffix}").write_bytes(data)
    s = get_settings()
    s.cv_filename, s.cv_text, s.cv_uploaded = cv.filename or "cv", text, utcnow().strftime("%d %b %Y %H:%M")
    save_settings(s)
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/settings/cv/delete")
def delete_cv():
    s = get_settings()
    s.cv_filename = s.cv_text = s.cv_uploaded = ""
    save_settings(s)
    _remove_cv_files()
    return RedirectResponse("/settings#profile", status_code=303)


@router.post("/settings/search", response_class=HTMLResponse)
def save_search(
    centres: str = Form(""), include_remote: bool = Form(False), remote_penalty: int = Form(5), min_salary: int = Form(0),
    max_age_days: int = Form(21), queries: str = Form(""), board_queries: str = Form(""), discovery_queries: str = Form(""),
    academic_queries: str = Form(""), country: str = Form("gb"), country_name: str = Form("United Kingdom"),
    currency_symbol: str = Form("£"),
):
    s = get_settings()
    parsed = []
    known = {c.name.lower(): c for c in s.search.centres}
    from .. import geo

    for line in _lines(centres):
        name, _, radius = line.rpartition(",")
        name, radius = (name or radius).strip(), radius.strip()
        r = float(radius) if radius.replace(".", "").isdigit() else 20
        if name.lower() in known:
            c = known[name.lower()]
            parsed.append(Centre(name=c.name, lat=c.lat, lon=c.lon, radius_mi=r))
            continue
        coords = geo.geocode(name, s.search.centres)
        if not coords:
            return _err(f'Couldn\'t find "{name}" - try a town name or postcode')
        parsed.append(Centre(name=name, lat=coords[0], lon=coords[1], radius_mi=r))
    s.search.centres = parsed or s.search.centres
    s.search.include_remote, s.search.remote_penalty = include_remote, remote_penalty
    s.search.min_salary, s.search.max_age_days = min_salary, max_age_days
    s.search.queries, s.search.board_queries = _lines(queries), _lines(board_queries)
    s.search.discovery_queries, s.search.academic_queries = _lines(discovery_queries), _lines(academic_queries)
    s.search.country = country.strip().lower()[:2] or "gb"
    s.search.country_name = country_name.strip() or "United Kingdom"
    s.search.currency_symbol = currency_symbol.strip()[:3] or "£"
    save_settings(s)
    changed = pipeline.refilter_all()
    return _saved(f"Saved · {changed['now_visible']} jobs now shown, {changed['now_filtered']} now filtered out")


@router.post("/settings/filters", response_class=HTMLResponse)
def save_filters(seniority_words: str = Form(""), exclude_keywords: str = Form(""), max_years_experience: int = Form(4),
                 category_weights: str = Form("")):
    s = get_settings()
    s.filters.seniority_words = [w.strip() for w in seniority_words.split(",") if w.strip()]
    s.filters.exclude_keywords = [w.strip() for w in exclude_keywords.split(",") if w.strip()]
    s.filters.max_years_experience = max_years_experience
    weights = {}
    for line in _lines(category_weights):
        k, _, v = line.partition(":")
        try:
            weights[k.strip()] = int(v.strip())
        except ValueError:
            return _err(f'Bad weight line: "{line}" (use Category: number)')
    s.filters.category_weights = weights
    save_settings(s)
    changed = pipeline.refilter_all()
    return _saved(f"Saved · {changed['now_visible']} jobs now shown, {changed['now_filtered']} now filtered out")


@router.post("/settings/sources", response_class=HTMLResponse)
async def save_sources(request: Request):
    form = await request.form()
    s = get_settings()
    for name in SOURCES:
        cfg = s.sources.get(name, SourceSettings())
        cfg.enabled = form.get(f"{name}_enabled") == "on"
        try:
            cfg.interval_hours = max(0.25, float(form.get(f"{name}_interval") or cfg.interval_hours))
        except ValueError:
            pass
        s.sources[name] = cfg
    save_settings(s)
    scheduler.sync_jobs()
    return _saved("Sources saved and rescheduled")


@router.post("/settings/watchlist", response_class=HTMLResponse)
def save_watchlist(watchlist: str = Form("")):
    s = get_settings()
    entries = []
    for line in _lines(watchlist):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or parts[1] not in ("greenhouse", "lever", "ashby", "smartrecruiters", "workday", "page"):
            return _err(f'Bad line: "{line}" - use Name | type | id-or-url')
        entries.append(WatchEntry(name=parts[0], type=parts[1], id=parts[2]))
    s.watchlist = entries
    save_settings(s)
    return _saved(f"Watchlist saved ({len(entries)} companies)")


@router.post("/settings/alerts", response_class=HTMLResponse)
def save_alerts(enabled: bool = Form(False), ha_url: str = Form(""), notify_service: str = Form(""), threshold: int = Form(80),
                quiet_start: str = Form("23:00"), quiet_end: str = Form("08:00"), public_url: str = Form(""),
                mode: str = Form("instant"), digest_time: str = Form("18:00"), ha_enabled: bool = Form(False),
                apprise_enabled: bool = Form(False)):
    s = get_settings()
    a = s.alerts
    a.enabled, a.ha_url, a.threshold = enabled, ha_url.strip(), threshold
    if ha_url.strip() and urlsplit(ha_url.strip()).scheme not in ("http", "https"):
        return _err("The Home Assistant URL must start with http:// or https://")
    a.mode, a.digest_time = (mode if mode in ("instant", "digest") else "instant"), digest_time
    a.ha_enabled, a.apprise_enabled = ha_enabled, apprise_enabled
    a.notify_service = notify_service.strip().removeprefix("notify.")
    a.quiet_start, a.quiet_end, a.public_url = quiet_start, quiet_end, public_url.strip() or a.public_url
    save_settings(s)
    return _saved("Alert settings saved")


@router.post("/settings/alerts/test", response_class=HTMLResponse)
def test_alert():
    s = get_settings()
    if not notify.configured(s):
        return _err("Nothing to send to yet: set APPRISE_URLS, or the Home Assistant URL, notify service and "
                    "HA_TOKEN - and make sure alerts are on")
    results = notify.send(s, "Job Scout test", "Alerts are working - strong matches will arrive like this.",
                          s.alerts.public_url)
    failed = {k: v for k, v in results.items() if v}
    if failed:
        return _err("; ".join(f"{k} failed: {v}" for k, v in failed.items()))
    return _saved(f"Test sent via {', '.join(results)}")


@router.post("/settings/llm", response_class=HTMLResponse)
def save_llm(score_model: str = Form(...), writer_model: str = Form(...), daily_limit: int = Form(300),
             daily_budget_usd: float = Form(2.0), firecrawl_daily_credits: int = Form(60),
             batch_rescore: bool = Form(False), prices: str = Form("")):
    s = get_settings()
    score_model, writer_model = score_model.strip(), writer_model.strip()
    for model in {score_model, writer_model} - {s.llm.score_model, s.llm.writer_model}:
        if problem := llm.check_model(model):  # catch typos before they stall scoring
            return _err(problem)
    table = {}
    for line in _lines(prices):
        model, _, nums = line.partition(":")
        try:
            p_in, p_out = (float(x) for x in nums.split(","))
        except ValueError:
            return _err(f'Bad price line: "{line}" (use model: input, output)')
        table[model.strip()] = [p_in, p_out]
    s.llm.score_model, s.llm.writer_model = score_model, writer_model
    s.llm.daily_limit, s.llm.firecrawl_daily_credits = max(daily_limit, 0), max(firecrawl_daily_credits, 0)
    s.llm.daily_budget_usd, s.llm.batch_rescore = max(daily_budget_usd, 0.0), batch_rescore
    s.llm.prices = table or s.llm.prices
    save_settings(s)
    return _saved("Saved")


@router.post("/settings/prompts", response_class=HTMLResponse)
def save_prompts(scoring_rubric: str = Form(""), writer_persona: str = Form(""), note_language: str = Form(""),
                 reset: str = Form("")):
    s = get_settings()
    if reset:
        s.llm.scoring_rubric, s.llm.writer_persona = DEFAULT_RUBRIC, DEFAULT_PERSONA
        save_settings(s)
        return RedirectResponse("/settings#prompts", status_code=303)
    s.llm.scoring_rubric = scoring_rubric.strip() or DEFAULT_RUBRIC
    s.llm.writer_persona = writer_persona.strip() or DEFAULT_PERSONA
    s.llm.note_language = note_language.strip() or "British English"
    save_settings(s)
    return _saved("Saved - use Re-score all to apply the new rubric to existing jobs")


@router.post("/settings/rescore", response_class=HTMLResponse)
def rescore():
    scheduler.run_in_background(pipeline.rescore_all)
    s = get_settings()
    if s.llm.batch_rescore:
        return _saved("Re-scoring in the background - large re-scores go through the Batches API and finish within a few hours")
    return _saved("Re-scoring in the background - jobs update as they're scored")


@router.post("/settings/reset", response_class=HTMLResponse)
def reset():
    reset_settings()
    scheduler.sync_jobs()
    return RedirectResponse("/settings", status_code=303)


@router.get("/healthz")
def healthz():
    with session() as s:
        n = s.exec(select(func.count()).select_from(Job)).one()
    return {"ok": True, "jobs": n, "scheduled": list(scheduler.next_runs())}
