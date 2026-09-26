"""Job boards without a public API, read through Firecrawl (rendered markdown) + Claude extraction.

Indeed and LinkedIn prohibit automated access in their terms, so those are off by default and
meant for low-volume personal use only. Each board page costs 1 Firecrawl credit and 1 LLM call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

from .. import llm
from ..config import AppSettings, Centre
from ..pages import firecrawl_markdown
from .base import RawJob, Source, log


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


@dataclass
class Board:
    name: str
    label: str
    url: str  # format fields: q, q_slug, loc, loc_slug, r
    note: str
    loc_suffix: str = ""

    def search_url(self, query: str, centre: Centre) -> str:
        loc = centre.name + self.loc_suffix
        return self.url.format(
            q=quote_plus(query), q_slug=_slug(query), loc=quote_plus(loc), loc_slug=_slug(centre.name), r=int(centre.radius_mi)
        )


BOARDS = [
    Board("findajob", "Find a Job (DWP)", "https://findajob.dwp.gov.uk/search?q={q}&w={loc}&d={r}&pp=50&f=7",
          "Government board. Many adverts redirect to the employer's own application page."),
    Board("indeed", "Indeed", "https://uk.indeed.com/jobs?q={q}&l={loc}&radius={r}&fromage=7&sort=date",
          "Terms forbid scraping - personal low-volume use only. Apply with your Indeed profile CV."),
    Board("gumtree", "Gumtree", "https://www.gumtree.com/search?search_category=jobs&q={q}&search_location={loc_slug}&distance={r}",
          "Small local employers. Apply via Gumtree's reply form with a short message and attached CV."),
    Board("totaljobs", "Totaljobs", "https://www.totaljobs.com/jobs/{q_slug}/in-{loc_slug}?radius={r}&postedWithin=7",
          "StepStone board. Apply with a Totaljobs profile CV."),
    Board("cwjobs", "CWJobs", "https://www.cwjobs.co.uk/jobs/{q_slug}/in-{loc_slug}?radius={r}&postedWithin=7",
          "IT-specialist StepStone board, lots of agency adverts. Apply with a CWJobs profile CV."),
    Board("linkedin", "LinkedIn", "https://www.linkedin.com/jobs/search?keywords={q}&location={loc}&distance={r}&f_TPR=r604800&f_E=1%2C2",
          "Terms forbid scraping - personal low-volume use only. Easy Apply or external link; message the poster too.",
          loc_suffix=", United Kingdom"),
    Board("nhs", "NHS Jobs", "https://www.jobs.nhs.uk/candidate/search/results?keyword={q}&location={loc}&distance={r}",
          "NHS digital/IT roles. Application form with a supporting statement against the person spec."),
]


class BoardSource(Source):
    kind = "scrape"

    def __init__(self, board: Board):
        self.board = board
        self.name = board.name
        self.label = board.label
        self.note = board.note

    def fetch(self, settings: AppSettings) -> list[RawJob]:
        jobs: list[RawJob] = []
        errors = []
        for centre in settings.search.centres:
            for q in settings.search.board_queries:
                url = self.board.search_url(q, centre)
                try:
                    md = firecrawl_markdown(settings, url)
                except Exception as e:  # keep going: one blocked page shouldn't sink the run
                    errors.append(f"{url}: {e}")
                    if "budget" in str(e):
                        break
                    continue
                if not md.strip():
                    continue
                for ej in llm.extract_jobs(settings, md, url, hint=f"This is a {self.label} search results page."):
                    jobs.append(
                        RawJob(title=ej.title, url=ej.url or url, source=self.name, company=ej.company,
                               location=ej.location, description=ej.summary, salary_text=ej.salary)
                    )
        if errors and not jobs:
            raise RuntimeError("; ".join(errors[:3]))
        for e in errors:
            log.warning("%s: %s", self.name, e)
        return jobs
