"""collect -> normalise -> dedupe/merge -> hard filters -> LLM score -> alert."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.exc import OperationalError
from sqlmodel import col, or_, select

from . import enrich, geo, llm, notify
from .config import AppSettings, get_settings, missing_keys
from .credentials import redact
from .db import Job, SourceRun, session, utcnow
from .profile import profile_text
from .sources import SOURCES, RawJob

log = logging.getLogger("jobscout.pipeline")

_ingest_lock = threading.Lock()
_score_lock = threading.Lock()
_source_locks: dict[str, threading.Lock] = {}
MAX_SCORE_ATTEMPTS = 3

TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|refId|trackingId|src$|source$)", re.I)
COMPANY_SUFFIX = re.compile(r"\b(ltd|limited|plc|llp|inc|group|holdings|uk|the|co)\b\.?", re.I)
NUM = r"(\d+(?:[.,]\d+)*)"
# "£25,000 - £30,000", "£28k", "£30-35k", "£30k to 35k" - the second figure's "k" applies to a bare first one.
MONEY = re.compile(rf"[£$€]\s*{NUM}\s*(k)?(?:\s*(?:-|–|—|to)\s*[£$€]?\s*{NUM}\s*(k)?)?", re.I)


# ---------------------------------------------------------------- normalisation

def clean_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", query, ""))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fingerprint(title: str, company: str, location: str, url: str) -> str:
    t = _norm(re.sub(r"\(.*?\)|\[.*?\]", " ", title))
    c = _norm(COMPANY_SUFFIX.sub(" ", company or ""))
    loc = _norm((location or "").split(",")[0])
    key = f"{t}|{c}|{loc}" if c else f"{t}|{clean_url(url)}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def parse_salary(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    vals = []
    for a, ak, b, bk in MONEY.findall(text):
        lo = float(a.replace(",", ""))
        hi = float(b.replace(",", "")) if b else None
        if hi is not None:
            if bk and not ak and lo < 1000:
                ak = bk
            if ak and not bk and hi < 1000:
                bk = ak
        vals.append(lo * 1000 if ak else lo)
        if hi is not None:
            vals.append(hi * 1000 if bk else hi)
    if not vals:
        return None, None
    t = text.lower()
    mult = 1950 if re.search(r"per hour|/hr|p/h|an hour|hourly", t) else 230 if re.search(r"per day|/day|daily|p/d", t) else 1
    vals = [v * mult for v in vals]
    return min(vals), max(vals)


# ---------------------------------------------------------------- filters

def _years_required(text: str) -> int | None:
    m = re.findall(r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*\d{0,2}\s*\+?\s*years?[’'`s]*\s+(?:of\s+)?(?:\w+\s+){0,2}experience", text, re.I)
    nums = [int(x) for x in m if 0 < int(x) < 30]
    return min(nums) if nums else None


def apply_filters(job: Job, settings: AppSettings) -> None:
    """Sets geo fields and filtered_out/filter_reason in place. Cheap, no LLM."""
    f, sch = settings.filters, settings.search
    title_l = job.title.lower()
    reason = ""

    job.remote = job.remote or geo.is_remote_text(job.title, job.location)
    if job.lat is None and job.location:
        coords = geo.geocode(job.location, sch.centres)
        if coords:
            job.lat, job.lon = coords
    if job.lat is not None:
        centre, d = geo.nearest(job.lat, job.lon, sch.centres)
        job.nearest_centre = centre.name if centre else None
        job.distance_mi = round(d, 1) if d is not None else None
        within = any(geo.haversine_mi(job.lat, job.lon, c.lat, c.lon) <= c.radius_mi for c in sch.centres)
        if not within:
            if not job.remote:
                reason = f"outside search area ({job.distance_mi:.0f} mi from {job.nearest_centre})"
            elif not sch.include_remote:
                reason = "remote roles switched off"
    elif job.remote and not sch.include_remote:
        reason = "remote roles switched off"

    if not reason:
        for w in f.seniority_words:
            if re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", title_l):
                reason = f"seniority: '{w}'"
                break
    if not reason:
        # title/company only - advert bodies mention "driver", "manager" etc. in passing
        blob = f"{job.title} {job.company}".lower()
        for w in f.exclude_keywords:
            if w.strip() and re.search(rf"(?<![a-z]){re.escape(w.strip().lower())}(?![a-z])", blob):
                reason = f"excluded keyword: '{w}'"
                break
    if not reason:
        yrs = _years_required(job.description)
        if yrs is not None and yrs > f.max_years_experience:
            reason = f"asks for {yrs}+ years"
    if not reason and sch.min_salary and (job.salary_max or job.salary_min):
        top = job.salary_max or job.salary_min
        if top < sch.min_salary:
            reason = f"salary below £{sch.min_salary:,}"
    if not reason and job.posted_at and job.posted_at < utcnow() - timedelta(days=sch.max_age_days):
        reason = f"older than {sch.max_age_days} days"

    job.filtered_out = bool(reason)
    job.filter_reason = reason


# ---------------------------------------------------------------- ingest

def ingest(raw_jobs: list[RawJob], settings: AppSettings) -> dict[str, int]:
    stats = {"found": len(raw_jobs), "new": 0, "merged": 0, "filtered": 0}
    now = utcnow()
    # Geocode before opening the write transaction: the cache writes use their own sessions.
    geo.warm({r.location for r in raw_jobs if r.lat is None}, settings.search.centres)
    with _ingest_lock, session() as s:
        batch: dict[str, Job] = {}
        for r in raw_jobs:
            url = clean_url(r.url or "")
            if not r.title or not url.startswith(("http://", "https://")):
                continue  # model-extracted or scraped links could be anything, e.g. javascript:
            if r.apply_url and not r.apply_url.startswith(("http://", "https://")):
                r.apply_url = ""
            fp = fingerprint(r.title, r.company, r.location, url)
            job = batch.get(fp) or s.exec(select(Job).where(or_(Job.fingerprint == fp, Job.url == url))).first()
            src = {"source": r.source, "url": url, "apply_url": r.apply_url, "seen_at": now.isoformat(timespec="seconds")}
            smin, smax = (r.salary_min, r.salary_max) if r.salary_min else parse_salary(r.salary_text)

            if job is None:
                job = Job(
                    fingerprint=fp, title=r.title.strip(), company=r.company.strip(), location=r.location.strip(),
                    lat=r.lat, lon=r.lon, remote=bool(r.remote), description=r.description.strip(),
                    salary_min=smin, salary_max=smax, salary_text=r.salary_text, url=url, apply_url=r.apply_url,
                    sources=[src], posted_at=r.posted_at, first_seen=now, last_seen=now,
                )
                apply_filters(job, settings)
                stats["new"] += 1
                stats["filtered"] += int(job.filtered_out)
            else:
                job.last_seen = now
                if not any(x["source"] == r.source and x["url"] == url for x in job.sources):
                    job.sources = [*job.sources, src]
                    stats["merged"] += 1
                if len(r.description) > len(job.description):
                    job.description = r.description.strip()
                if not job.salary_min and smin:
                    job.salary_min, job.salary_max, job.salary_text = smin, smax, r.salary_text
                if job.lat is None and r.lat is not None:
                    job.lat, job.lon = r.lat, r.lon
                if not job.apply_url and r.apply_url:
                    job.apply_url = r.apply_url
            batch[fp] = job
            s.add(job)
        s.commit()
    return stats


# ---------------------------------------------------------------- scoring

def _score_hash(job: Job, profile: str) -> str:
    return hashlib.sha1(f"{job.title}\n{job.company}\n{job.description}\n{profile}".encode()).hexdigest()[:16]


def _job_payload(job: Job) -> dict:
    return {
        "source": ", ".join(sorted({x["source"] for x in job.sources})),
        "title": job.title, "company": job.company, "location": job.location,
        "salary": job.salary_text, "url": job.url, "description": job.description,
        "apply_method": job.apply_method,
    }


def score_job(job: Job, settings: AppSettings, profile: str) -> None:
    a = llm.score_job(settings, profile, _job_payload(job))
    # Web-search results arrive with jumbled titles and no location - take the model's reading of the advert.
    from_search = all(x["source"] == "brave" for x in job.sources)
    if from_search and a.advert_title.strip():
        job.title = a.advert_title.strip()
    if a.advert_company.strip() and (from_search or not job.company):
        job.company = a.advert_company.strip()
    if a.advert_location.strip() and (from_search or not job.location) and a.advert_location.strip() != job.location:
        job.location = a.advert_location.strip()
        job.lat = job.lon = None
        apply_filters(job, settings)  # re-geocode and re-check the search area
    weight = settings.filters.category_weights.get(a.category, 0)
    remote_pen = settings.search.remote_penalty if (a.is_remote or job.remote) and job.lat is None else 0
    job.score = max(0, min(100, a.fit_score + weight - remote_pen))
    job.category = a.category
    job.why = a.why
    job.cv_angle = a.cv_angle
    job.seniority_fit = a.seniority_fit
    job.red_flags = a.red_flags
    job.apply_method = a.apply_method
    job.apply_steps = a.apply_steps
    job.contacts = [c.model_dump() for c in a.contacts]
    job.remote = job.remote or a.is_remote
    job.score_hash = _score_hash(job, profile)
    job.scored_at = utcnow()
    job.score_attempts = 0


def score_pending(settings: AppSettings | None = None, max_jobs: int = 500) -> int:
    """Score unscored, unfiltered jobs - nearest first. Loops until none remain or budget hits."""
    if not llm.available():
        return 0
    if not _score_lock.acquire(blocking=False):
        return 0  # another thread is already draining the queue
    scored = 0
    try:
        settings = settings or get_settings()
        profile = profile_text(settings)
        while scored < max_jobs:
            with session() as s:
                job = s.exec(
                    select(Job)
                    .where(Job.score == None, Job.filtered_out == False, Job.status != "hidden")  # noqa: E711,E712
                    .order_by(Job.score_attempts, Job.distance_mi == None, Job.distance_mi,  # noqa: E711
                              Job.first_seen.desc())
                ).first()
                if job is None:
                    break
                try:
                    score_job(job, settings, profile)
                    if job.score >= settings.alerts.threshold and not job.company_website:
                        try:
                            enrich.enrich_company(job)
                        except Exception as e:
                            log.info("enrich failed for %s: %s", job.company, e)
                except llm.BudgetExceeded as e:
                    log.info("%s", e)
                    break
                except llm.SYSTEMIC_ERRORS as e:
                    # outage, rate limit, bad key or model name: not this job's fault - leave it queued
                    log.warning("scoring paused: %s", redact(str(e)))
                    break
                except Exception as e:
                    log.warning("scoring job %s failed: %s", job.id, redact(str(e)))
                    job.score_attempts += 1
                    if job.score_attempts >= MAX_SCORE_ATTEMPTS:
                        job.score, job.why = 0, f"(scoring failed: {redact(str(e))})"
                s.add(job)
                try:
                    s.commit()
                except OperationalError as e:  # a long ingest holds the write lock - back off, retry later
                    log.info("score commit deferred: %s", e)
                    s.rollback()
                    time.sleep(5)
                    continue
                scored += 1
    finally:
        _score_lock.release()
    notify.flush_alerts()
    return scored


def rescore_all() -> int:
    with session() as s:
        active = select(Job).where(Job.filtered_out == False, col(Job.status).not_in(["hidden", "rejected"]))  # noqa: E712
        for job in s.exec(active).all():
            job.score, job.score_attempts = None, 0
            s.add(job)
        s.commit()
    return score_pending()


def refilter_all() -> dict[str, int]:
    settings = get_settings()
    changed = {"now_filtered": 0, "now_visible": 0}
    with session() as s:
        geo.warm({j.location for j in s.exec(select(Job).where(Job.lat == None)).all()}, settings.search.centres)  # noqa: E711
    with _ingest_lock, session() as s:
        for job in s.exec(select(Job)).all():
            before = job.filtered_out
            apply_filters(job, settings)
            if job.filtered_out != before:
                changed["now_filtered" if job.filtered_out else "now_visible"] += 1
                s.add(job)
        s.commit()
    return changed


# ---------------------------------------------------------------- runs

def run_source(name: str) -> SourceRun:
    source = SOURCES[name]
    lock = _source_locks.setdefault(name, threading.Lock())
    run = SourceRun(source=name)
    if not lock.acquire(blocking=False):
        run.error = "already running"
        return run
    try:
        settings = get_settings()
        with session() as s:
            s.add(run)
            s.commit()
        missing = missing_keys(name)
        if missing:
            raise RuntimeError(f"missing {', '.join(missing)} in .env")
        raw = source.fetch(settings)
        stats = ingest(raw, settings)
        run.found, run.new, run.merged, run.filtered = stats["found"], stats["new"], stats["merged"], stats["filtered"]
        log.info("%s: %s", name, stats)
    except Exception as e:
        log.exception("source %s failed", name)
        run.error = redact(str(e))[:1000]
    finally:
        run.finished_at = utcnow()
        with session() as s:
            s.add(run)
            s.commit()
        lock.release()
    if not run.error or run.new:
        run.scored = score_pending()
        with session() as s:
            s.add(run)
            s.commit()
    return run
