from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..config import AppSettings
from ..net import http_client  # noqa: F401  (re-exported for collectors)

log = logging.getLogger("jobscout.sources")


@dataclass
class RawJob:
    title: str
    url: str
    source: str
    company: str = ""
    location: str = ""
    description: str = ""
    apply_url: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_text: str = ""
    posted_at: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    remote: bool | None = None
    extra: dict = field(default_factory=dict)


class Source:
    """A collector. Subclasses implement fetch(); the pipeline does everything else."""

    name: str = ""
    label: str = ""
    kind: str = "api"  # api | scrape | discovery
    note: str = ""

    def fetch(self, settings: AppSettings) -> list[RawJob]:
        raise NotImplementedError

