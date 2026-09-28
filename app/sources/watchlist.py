"""Company watchlist. Uses each employer's applicant-tracking system's public JSON API where one
exists (free, exact, no scraping); falls back to reading the careers page and extracting with
Claude, which only runs when the page text has changed since last time.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from .. import llm
from ..config import AppSettings, WatchEntry
from ..db import SeenUrl, session, utcnow
from ..pages import get_page_text
from .base import RawJob, Source, http_client, log


def _strip_html(s: str) -> str:
    import html as _html

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _html.unescape(s or ""))).strip()


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, UTC).replace(tzinfo=None)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, OSError):
        return None


def greenhouse(c, e: WatchEntry, settings: AppSettings) -> list[RawJob]:
    r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{e.id}/jobs", params={"content": "true"})
    r.raise_for_status()
    return [
        RawJob(title=j["title"], url=j["absolute_url"], source="watchlist", company=e.name,
               location=(j.get("location") or {}).get("name", ""), description=_strip_html(j.get("content", ""))[:6000],
               posted_at=_ts(j.get("first_published") or j.get("updated_at")))
        for j in r.json().get("jobs", [])
    ]


def lever(c, e: WatchEntry, settings: AppSettings) -> list[RawJob]:
    r = c.get(f"https://api.lever.co/v0/postings/{e.id}", params={"mode": "json"})
    r.raise_for_status()
    return [
        RawJob(title=j["text"], url=j["hostedUrl"], apply_url=j.get("applyUrl", ""), source="watchlist", company=e.name,
               location=(j.get("categories") or {}).get("location", ""), description=(j.get("descriptionPlain") or "")[:6000],
               posted_at=_ts(j.get("createdAt")), remote=(j.get("workplaceType") == "remote") or None)
        for j in r.json()
    ]


def ashby(c, e: WatchEntry, settings: AppSettings) -> list[RawJob]:
    r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{e.id}")
    r.raise_for_status()
    return [
        RawJob(title=j["title"], url=j["jobUrl"], apply_url=j.get("applyUrl", ""), source="watchlist", company=e.name,
               location=j.get("location", ""), description=(j.get("descriptionPlain") or "")[:6000],
               posted_at=_ts(j.get("publishedAt")), remote=j.get("isRemote"))
        for j in r.json().get("jobs", [])
    ]


def smartrecruiters(c, e: WatchEntry, settings: AppSettings) -> list[RawJob]:
    r = c.get(f"https://api.smartrecruiters.com/v1/companies/{e.id}/postings",
              params={"limit": 100, "country": settings.search.country.lower()})
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        out.append(RawJob(title=j["name"], url=f"https://jobs.smartrecruiters.com/{e.id}/{j['id']}", source="watchlist",
                          company=e.name, location=", ".join(x for x in [loc.get("city"), loc.get("region")] if x),
                          posted_at=_ts(j.get("releasedDate")), remote=loc.get("remote")))
    return out


def _workday_posted(text: str) -> datetime | None:
    """Workday only says "Posted Today", "Posted Yesterday", "Posted 3 Days Ago" or "Posted 30+ Days Ago"."""
    t = (text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    elif m := re.search(r"(\d+)\+?\s*days?", t):
        days = int(m.group(1))
    else:
        return None
    return utcnow() - timedelta(days=days)


def workday(c, e: WatchEntry, settings: AppSettings) -> list[RawJob]:
    tenant, wd, site = e.id.split("/")
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    out = []
    for offset in range(0, 200, 20):
        r = c.post(f"{host}/wday/cxs/{tenant}/{site}/jobs",
                   json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": settings.search.country_name})
        r.raise_for_status()
        data = r.json()
        posts = data.get("jobPostings", [])
        for j in posts:
            if not j.get("externalPath"):
                continue
            out.append(RawJob(title=j["title"], url=f"{host}/{site}{j['externalPath']}", source="watchlist", company=e.name,
                              location=j.get("locationsText", ""), posted_at=_workday_posted(j.get("postedOn", ""))))
        if offset + 20 >= (data.get("total") or 0) or not posts:
            break
    return out


def page(c, e: WatchEntry, settings: AppSettings) -> list[RawJob]:
    text, via = get_page_text(settings, e.id)
    if not text:
        raise RuntimeError(f"could not read {e.id}")
    digest = hashlib.sha256(text.encode()).hexdigest()
    with session() as s:
        row = s.get(SeenUrl, e.id)
        if row is not None and row.content_hash == digest:
            return []  # unchanged since last run - nothing new to extract
        extracted = llm.extract_jobs(settings, text, e.id, hint=f"This is the careers page of {e.name}.")
        row = row or SeenUrl(url=e.id)
        row.content_hash = digest
        row.seen_at = utcnow()
        s.add(row)
        s.commit()
    return [
        RawJob(title=j.title, url=j.url or e.id, source="watchlist", company=j.company or e.name, location=j.location,
               description=j.summary, salary_text=j.salary)
        for j in extracted
    ]


HANDLERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters, "workday": workday,
            "page": page}


class Watchlist(Source):
    name = "watchlist"
    label = "Company watchlist"
    kind = "api"
    note = "Straight from employers' own careers systems. Apply on the company's site."

    def fetch(self, settings: AppSettings) -> list[RawJob]:
        jobs: list[RawJob] = []
        errors = []
        with http_client(headers={"Accept": "application/json"}) as c:
            for e in settings.watchlist:
                try:
                    jobs += HANDLERS[e.type](c, e, settings)
                except Exception as ex:
                    errors.append(f"{e.name}: {ex}")
                    log.warning("watchlist %s failed: %s", e.name, ex)
        if errors and not jobs:
            raise RuntimeError("; ".join(errors[:5]))
        return jobs
