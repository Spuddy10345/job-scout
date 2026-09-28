"""Runtime settings, layered: config/defaults.yaml < config/local.yaml (optional, gitignored,
path overridable with JOBSCOUT_CONFIG) < whatever was saved from the Settings page (Setting table).

Secrets never live here - they come from the environment (.env) only.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .credentials import ROOT, env
from .db import Setting, session
from .prompts import DEFAULT_CATEGORIES, DEFAULT_PERSONA, DEFAULT_RUBRIC, NOT_RELEVANT

DEFAULTS_PATH = ROOT / "config" / "defaults.yaml"
LOCAL_PATH = Path(os.environ.get("JOBSCOUT_CONFIG") or ROOT / "config" / "local.yaml")


class Centre(BaseModel):
    name: str
    lat: float
    lon: float
    radius_mi: float = 20


class SearchSettings(BaseModel):
    centres: list[Centre] = Field(default_factory=list)
    include_remote: bool = True
    remote_penalty: int = 5
    min_salary: int = 0
    max_age_days: int = 21
    queries: list[str] = Field(default_factory=list)
    board_queries: list[str] = Field(default_factory=list)
    discovery_queries: list[str] = Field(default_factory=list)
    academic_queries: list[str] = Field(default_factory=list)
    country: str = "gb"  # Adzuna country code and SmartRecruiters filter
    country_name: str = "United Kingdom"  # Workday search text
    currency_symbol: str = "£"


class FilterSettings(BaseModel):
    seniority_words: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    max_years_experience: int = 4
    category_weights: dict[str, int] = Field(default_factory=dict)


class SourceSettings(BaseModel):
    enabled: bool = True
    interval_hours: float = 12


class WatchEntry(BaseModel):
    name: str
    type: str  # greenhouse | lever | ashby | smartrecruiters | workday | page
    id: str


class AlertSettings(BaseModel):
    enabled: bool = True
    ha_url: str = ""
    notify_service: str = ""
    threshold: int = 80
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"
    public_url: str = "http://localhost:8080"


class LLMSettings(BaseModel):
    score_model: str = "claude-haiku-4-5"
    writer_model: str = "claude-sonnet-5"
    daily_limit: int = 300
    firecrawl_daily_credits: int = 60
    scoring_rubric: str = DEFAULT_RUBRIC
    writer_persona: str = DEFAULT_PERSONA


class AppSettings(BaseModel):
    profile_mode: str = "both"
    profile_md: str = ""
    cv_filename: str = ""
    cv_text: str = ""
    cv_uploaded: str = ""
    search: SearchSettings = Field(default_factory=SearchSettings)
    filters: FilterSettings = Field(default_factory=FilterSettings)
    sources: dict[str, SourceSettings] = Field(default_factory=dict)
    watchlist: list[WatchEntry] = Field(default_factory=list)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)


# Which environment variables each source needs before it can run.
SOURCE_KEYS: dict[str, list[str]] = {
    "adzuna": ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"],
    "reed": ["REED_API_KEY"],
    "jobs_ac_uk": [],
    "findajob": ["FIRECRAWL_API_KEY"],
    "indeed": ["FIRECRAWL_API_KEY"],
    "gumtree": ["FIRECRAWL_API_KEY"],
    "totaljobs": ["FIRECRAWL_API_KEY"],
    "cwjobs": ["FIRECRAWL_API_KEY"],
    "linkedin": ["FIRECRAWL_API_KEY"],
    "nhs": ["FIRECRAWL_API_KEY"],
    "brave": ["BRAVE_API_KEY"],
    "watchlist": [],
}


def missing_keys(source: str) -> list[str]:
    return [k for k in SOURCE_KEYS.get(source, []) if not env(k)]


_lock = threading.Lock()
_cache: AppSettings | None = None


def overlay(base: dict, over: dict) -> dict:
    """Section-level merge: `over` wins per field inside each section (search, filters, llm...), so
    fields added in newer versions keep their defaults, while lists and dicts such as category
    weights are replaced whole. Sources merge per source so new ones appear for existing installs."""
    merged = dict(base)
    for key, value in (over or {}).items():
        if key == "sources" and isinstance(value, dict):
            merged[key] = {**base.get(key, {}), **value}
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = {**base[key], **value}
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def defaults() -> AppSettings:
    data = _load_yaml(DEFAULTS_PATH)
    if LOCAL_PATH.exists():
        data = overlay(data, _load_yaml(LOCAL_PATH))
    return AppSettings.model_validate(data)


def get_settings() -> AppSettings:
    global _cache
    with _lock:
        if _cache is None:
            with session() as s:
                row = s.get(Setting, "settings")
            base = defaults()
            _cache = base if row is None else AppSettings.model_validate(overlay(base.model_dump(), row.value))
        return _cache.model_copy(deep=True)


def cached_settings() -> AppSettings:
    """Shared, read-only settings for hot paths like template filters - never mutate the result."""
    return _cache or (get_settings() and _cache)


def currency() -> str:
    return cached_settings().search.currency_symbol


def categories(settings: AppSettings) -> list[str]:
    cats = [c for c in settings.filters.category_weights if c != NOT_RELEVANT] or list(DEFAULT_CATEGORIES)
    return [*cats, NOT_RELEVANT]


def settings_saved() -> bool:
    with session() as s:
        return s.get(Setting, "settings") is not None


def save_settings(new: AppSettings) -> None:
    global _cache
    with _lock:
        with session() as s:
            row = s.get(Setting, "settings")
            if row is None:
                row = Setting(key="settings", value=new.model_dump())
            else:
                row.value = new.model_dump()
            s.add(row)
            s.commit()
        _cache = new.model_copy(deep=True)


def reset_settings() -> None:
    current, fresh = get_settings(), defaults()
    fresh.cv_filename, fresh.cv_text, fresh.cv_uploaded = current.cv_filename, current.cv_text, current.cv_uploaded
    save_settings(fresh)


def get_kv(key: str, default=None):
    with session() as s:
        row = s.get(Setting, key)
        return default if row is None else row.value


def set_kv(key: str, value) -> None:
    with session() as s:
        row = s.get(Setting, key) or Setting(key=key, value=value)
        row.value = value
        s.add(row)
        s.commit()
