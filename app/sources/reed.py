"""Reed.co.uk jobseeker API (free key: reed.co.uk/developers/jobseeker)."""

from __future__ import annotations

from datetime import datetime

from ..config import AppSettings, env
from .base import RawJob, Source, http_client, log

API = "https://www.reed.co.uk/api/1.0/search"


class Reed(Source):
    name = "reed"
    label = "Reed"
    note = "Official API. Apply on reed.co.uk with a Reed profile / uploaded CV."

    def fetch(self, settings: AppSettings) -> list[RawJob]:
        jobs: list[RawJob] = []
        with http_client(auth=(env("REED_API_KEY"), "")) as client:
            for c in settings.search.centres:
                for q in settings.search.queries:
                    params = {
                        "keywords": q,
                        "locationName": c.name,
                        "distanceFromLocation": int(c.radius_mi),
                        "resultsToTake": 100,
                    }
                    r = client.get(API, params=params)
                    if r.status_code != 200:
                        log.warning("reed %s -> %s", params, r.status_code)
                        r.raise_for_status()
                    for it in r.json().get("results", []):
                        jobs.append(self._parse(it))
        return jobs

    def _parse(self, it: dict) -> RawJob:
        posted = None
        if it.get("date"):
            try:
                posted = datetime.strptime(it["date"], "%d/%m/%Y")
            except ValueError:
                pass
        smin, smax = it.get("minimumSalary"), it.get("maximumSalary")
        salary_text = ""
        if smin:
            salary_text = f"£{smin:,.0f}" + (f" – £{smax:,.0f}" if smax and smax != smin else "")
        return RawJob(
            title=(it.get("jobTitle") or "").strip(),
            url=it.get("jobUrl", ""),
            source=self.name,
            company=it.get("employerName", ""),
            location=it.get("locationName", ""),
            description=it.get("jobDescription", ""),
            salary_min=smin,
            salary_max=smax,
            salary_text=salary_text,
            posted_at=posted,
        )
