from .adzuna import Adzuna
from .base import RawJob, Source
from .boards import BOARDS, BoardSource
from .brave import Brave
from .jobs_ac_uk import JobsAcUk
from .reed import Reed
from .watchlist import Watchlist

SOURCES: dict[str, Source] = {
    s.name: s
    for s in [Adzuna(), Reed(), JobsAcUk(), *(BoardSource(b) for b in BOARDS), Brave(), Watchlist()]
}

__all__ = ["SOURCES", "RawJob", "Source"]
