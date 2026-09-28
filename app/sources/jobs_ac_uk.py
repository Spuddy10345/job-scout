"""jobs.ac.uk - UK university and research roles (research software engineers, lab and IT posts).

The site's location filter is ignored server-side, so we search by keyword and let the
pipeline's radius filter keep the nearby ones.
"""

from __future__ import annotations

from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..config import AppSettings
from .base import RawJob, Source, http_client

SEARCH = "https://www.jobs.ac.uk/search/"
KEYWORDS = ["software engineer", "research software", "developer", "IT support", "data"]  # if none are configured


def parse_search(html: str, source: str = "jobs_ac_uk") -> list[RawJob]:
    tree = HTMLParser(html)
    jobs = []
    for node in tree.css("div.j-search-result__result"):
        a = node.css_first("div.j-search-result__text > a")
        if a is None:
            continue
        employer = node.css_first(".j-search-result__employer")
        dept = node.css_first(".j-search-result__department")
        salary = node.css_first(".j-search-result__info")
        location = ""
        for div in node.css("div.j-search-result__text > div"):
            text = div.text(strip=True)
            if text.startswith("Location:"):
                location = text.removeprefix("Location:").strip()
        salary_text = salary.text(separator=" ", strip=True).removeprefix("Salary:").strip() if salary else ""
        jobs.append(
            RawJob(
                title=a.text(strip=True),
                url=urljoin("https://www.jobs.ac.uk", a.attributes.get("href", "")),
                source=source,
                company=employer.text(strip=True) if employer else "",
                location=location,
                description=(dept.text(strip=True) if dept else ""),
                salary_text=" ".join(salary_text.split()),
            )
        )
    return jobs


class JobsAcUk(Source):
    name = "jobs_ac_uk"
    label = "jobs.ac.uk"
    kind = "scrape"
    note = "University & research posts. Applications go through each university's own portal."

    def fetch(self, settings: AppSettings) -> list[RawJob]:
        jobs: list[RawJob] = []
        with http_client() as client:
            for kw in settings.search.academic_queries or KEYWORDS:
                r = client.get(SEARCH, params={"keywords": kw, "sortOrder": "1", "pageSize": "100"})
                r.raise_for_status()
                jobs += parse_search(r.text, self.name)
        return jobs
