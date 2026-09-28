"""Adzuna search API (free key: developer.adzuna.com). Aggregates many boards incl. agency sites.

Covers ~20 countries; the country code comes from Settings → Search area.
"""

from __future__ import annotations

from datetime import datetime

from ..config import AppSettings, env
from .base import RawJob, Source, http_client, log

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
MI_TO_KM = 1.609


class Adzuna(Source):
    name = "adzuna"
    label = "Adzuna"
    note = "Official API. Aggregates Indeed-style listings from many UK boards and agencies."

    def fetch(self, settings: AppSettings) -> list[RawJob]:
        base = {
            "app_id": env("ADZUNA_APP_ID"),
            "app_key": env("ADZUNA_APP_KEY"),
            "results_per_page": 50,
            "max_days_old": settings.search.max_age_days,
            "sort_by": "date",
            "content-type": "application/json",
        }
        searches: list[dict] = []
        for c in settings.search.centres:
            where = {"where": c.name, "distance": round(c.radius_mi * MI_TO_KM)}
            # All IT jobs nearby (two pages), plus engineering roles that touch software/hardware.
            searches += [
                {**where, "category": "it-jobs", "_page": 1},
                {**where, "category": "it-jobs", "_page": 2},
                {**where, "what_or": "software firmware embedded electronics FPGA cryptography security developer programmer", "_page": 1},
                {**where, "what_or": "graduate junior trainee apprentice entry", "category": "it-jobs", "_page": 1},
            ]
        if settings.search.include_remote:
            searches.append({"what": "remote", "what_or": "graduate junior entry trainee", "category": "it-jobs", "_page": 1})

        jobs: list[RawJob] = []
        with http_client() as client:
            for s in searches:
                page = s.pop("_page")
                r = client.get(API.format(country=settings.search.country.lower() or "gb", page=page), params={**base, **s})
                if r.status_code != 200:
                    # never raise_for_status() here: its message includes the URL, and the key is in the query
                    log.warning("adzuna %s -> %s %s", s, r.status_code, r.text[:200])
                    raise RuntimeError(f"Adzuna HTTP {r.status_code}: {r.text[:120]}")
                for it in r.json().get("results", []):
                    jobs.append(self._parse(it))
        return jobs

    def _parse(self, it: dict) -> RawJob:
        predicted = str(it.get("salary_is_predicted", "0")) == "1"
        smin, smax = it.get("salary_min"), it.get("salary_max")
        posted = None
        if it.get("created"):
            try:
                posted = datetime.fromisoformat(it["created"].replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                pass
        salary_text = ""
        if smin and not predicted:
            salary_text = f"£{smin:,.0f}" + (f" – £{smax:,.0f}" if smax and smax != smin else "")
        return RawJob(
            title=it.get("title", "").strip(),
            url=it.get("redirect_url", ""),
            source=self.name,
            company=(it.get("company") or {}).get("display_name", ""),
            location=(it.get("location") or {}).get("display_name", ""),
            description=it.get("description", ""),
            salary_min=None if predicted else smin,
            salary_max=None if predicted else smax,
            salary_text=salary_text,
            posted_at=posted,
            lat=it.get("latitude"),
            lon=it.get("longitude"),
        )
