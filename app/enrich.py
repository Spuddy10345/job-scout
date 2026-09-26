"""Company enrichment via Brave Search: official website + LinkedIn page."""

from __future__ import annotations

from urllib.parse import urlsplit

from .config import env
from .db import Job
from .net import brave_search, http_client

JOB_SITES = ("indeed.", "reed.co.uk", "totaljobs", "cwjobs", "adzuna", "glassdoor", "gumtree", "linkedin.com/jobs",
             "findajob", "jobs.ac.uk", "wikipedia", "companieshouse", "find-and-update.company-information", "facebook.")


def available() -> bool:
    return bool(env("BRAVE_API_KEY"))


def enrich_company(job: Job) -> None:
    if not job.company or not available():
        return
    with http_client(headers={"X-Subscription-Token": env("BRAVE_API_KEY"), "Accept": "application/json"}) as c:
        data = brave_search(c, {"q": f"{job.company} {job.location.split(',')[0]} company", "country": "GB", "count": 10})
        results = (data.get("web") or {}).get("results", [])
    for res in results:
        url = res.get("url", "")
        low = url.lower()
        if not job.company_linkedin and "linkedin.com/company/" in low:
            job.company_linkedin = url
        elif not job.company_website and not any(s in low for s in JOB_SITES) and "linkedin.com" not in low:
            parts = urlsplit(url)
            job.company_website = f"{parts.scheme}://{parts.netloc}"
