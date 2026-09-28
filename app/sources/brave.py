"""Brave Search discovery: finds individual postings the boards miss (Civil Service, company
sites, graduate schemes). Only URLs that look like a single vacancy are kept; each URL is
handled once.
"""

from __future__ import annotations

import html
import re

from sqlmodel import select

from ..config import AppSettings, env
from ..db import SeenUrl, session
from ..net import brave_search
from ..pages import plain_text
from .base import RawJob, Source, http_client

POSTING_PATTERNS = [
    r"/jobs?/[^/?#]*\d",            # /job/1234-title, /jobs/abc123
    r"/vacanc(y|ies)/.+",
    r"/careers?/.+/(job|vacancy|opening)",
    r"civilservicejobs\.service\.gov\.uk/.*jcode=",
    r"jobs\.ac\.uk/job/",
    r"myworkdayjobs\.com/.+/job/",
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com)/.+",
    r"reed\.co\.uk/jobs/.+/\d+",
    r"jobs\.nhs\.uk/candidate/jobadvert/",
    r"/job-details?/",
    r"[?&](jobid|job_id|vacancyid)=",
]
# Search/listing pages on aggregators - not a single vacancy.
LISTING_PATTERNS = [r"indeed\.[a-z.]+/(jobs|q-)", r"/jobs/?\?", r"glassdoor", r"/search", r"linkedin\.com/jobs/[a-z-]+-jobs"]


def looks_like_posting(url: str) -> bool:
    u = url.lower()
    if any(re.search(p, u) for p in LISTING_PATTERNS):
        return False
    return any(re.search(p, u) for p in POSTING_PATTERNS)


def split_title(title: str) -> tuple[str, str]:
    """'Graduate Software Engineer - Acme Ltd | Careers' -> ('Graduate Software Engineer', 'Acme Ltd')."""
    parts = [p.strip() for p in re.split(r"\s[|\-–—]\s", title) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return title.strip(), ""


class Brave(Source):
    name = "brave"
    label = "Brave discovery"
    kind = "discovery"
    note = "Web search for postings on company and government sites. Apply wherever the link lands."

    def fetch(self, settings: AppSettings) -> list[RawJob]:
        jobs: list[RawJob] = []
        with session() as s:
            seen = set(s.exec(select(SeenUrl.url)).all())
        new_seen = []
        with http_client(headers={"X-Subscription-Token": env("BRAVE_API_KEY"), "Accept": "application/json"}) as c:
            for q in settings.search.discovery_queries:
                data = brave_search(c, {"q": q, "country": "GB", "search_lang": "en", "count": 20, "freshness": "pm"})
                for res in (data.get("web") or {}).get("results", []):
                    url = res.get("url", "")
                    if not url or url in seen or not looks_like_posting(url):
                        continue
                    seen.add(url)
                    new_seen.append(url)
                    title, company = split_title(html.unescape(res.get("title", "")))
                    desc = html.unescape(re.sub(r"<[^>]+>", "", res.get("description", "")))
                    extra = " ".join(html.unescape(re.sub(r"<[^>]+>", "", x)) for x in res.get("extra_snippets", []) or [])
                    jobs.append(RawJob(title=title, url=url, source=self.name, company=company,
                                       description=f"{desc} {extra}".strip()))
        for job in jobs:  # snippets are short and rarely say where - read the posting itself (free)
            text = plain_text(job.url)
            if len(text) > len(job.description) + 200:
                job.description = f"{job.description}\n\n{text[:8000]}"
        with session() as s:
            for u in new_seen:
                s.add(SeenUrl(url=u))
            s.commit()
        return jobs
