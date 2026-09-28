"""SQLite storage via SQLModel. One file, WAL mode, safe to share across scheduler threads."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import NaiveDatetime
from sqlalchemy import event, inspect, text
from sqlmodel import JSON, Column, Field, Session, SQLModel, create_engine

from .credentials import ROOT

DATA_DIR = Path(os.environ.get("JOBSCOUT_DATA") or ROOT / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATUSES = ["new", "interested", "applied", "interview", "offer", "rejected", "hidden"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_today() -> str:
    """Key for daily budgets and usage counters (UTC, like every stored timestamp)."""
    return utcnow().date().isoformat()


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    fingerprint: str = Field(index=True, unique=True)

    title: str
    company: str = ""
    location: str = ""
    lat: float | None = None
    lon: float | None = None
    distance_mi: float | None = None
    nearest_centre: str | None = None
    remote: bool = False

    salary_min: float | None = None
    salary_max: float | None = None
    salary_text: str = ""
    description: str = ""

    url: str = Field(index=True)
    apply_url: str = ""
    # [{source, url, apply_url, seen_at}]
    sources: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))

    posted_at: NaiveDatetime | None = None
    first_seen: NaiveDatetime = Field(default_factory=utcnow, index=True)
    last_seen: NaiveDatetime = Field(default_factory=utcnow)

    status: str = Field(default="new", index=True)
    filtered_out: bool = Field(default=False, index=True)
    filter_reason: str = ""

    # LLM assessment
    score: int | None = Field(default=None, index=True)
    category: str | None = None
    why: str = ""
    cv_angle: str = ""
    seniority_fit: str = ""
    red_flags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    apply_method: str = ""
    apply_steps: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    contacts: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    score_hash: str = ""
    scored_at: NaiveDatetime | None = None
    score_attempts: int = 0
    batch_id: str = Field(default="", index=True)  # set while queued in a Message Batch

    company_website: str = ""
    company_linkedin: str = ""
    cover_note: str = ""
    notified: bool = False


class JobEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(index=True, foreign_key="job.id")
    at: NaiveDatetime = Field(default_factory=utcnow)
    kind: str  # status | note
    text: str


class SourceRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    started_at: NaiveDatetime = Field(default_factory=utcnow, index=True)
    finished_at: NaiveDatetime | None = None
    found: int = 0
    new: int = 0
    merged: int = 0
    filtered: int = 0
    scored: int = 0
    error: str = ""


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: Any = Field(sa_column=Column(JSON))


class GeoCache(SQLModel, table=True):
    query: str = Field(primary_key=True)
    lat: float | None = None
    lon: float | None = None
    looked_up: NaiveDatetime = Field(default_factory=utcnow)


class SeenUrl(SQLModel, table=True):
    """URLs already handled by discovery sources (Brave), and content hashes for watched pages."""

    url: str = Field(primary_key=True)
    content_hash: str = ""
    seen_at: NaiveDatetime = Field(default_factory=utcnow)


engine = create_engine(
    f"sqlite:///{DATA_DIR / 'jobs.db'}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all() never alters existing tables, so add columns introduced since the DB was made.

    Additive only: new columns are nullable with their Python default as the SQL default.
    """
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}'
                default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
                if isinstance(default, bool):
                    ddl += f" DEFAULT {int(default)}"
                elif isinstance(default, (int, float)):
                    ddl += f" DEFAULT {default}"
                elif isinstance(default, str):
                    ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                conn.execute(text(ddl))
            for index in table.indexes:
                index.create(conn, checkfirst=True)


def session() -> Session:
    return Session(engine, expire_on_commit=False)
