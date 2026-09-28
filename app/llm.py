"""All Claude calls: job scoring, job extraction from pages, cover-note drafting.

Structured outputs (messages.parse + Pydantic) guarantee schema-valid JSON, so the pipeline
never has to repair model output. A per-day call budget guards against runaway spend.
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from typing import Literal

import anthropic
from pydantic import BaseModel, create_model

from .config import AppSettings, categories, env, get_kv, set_kv
from .db import utc_today
from .prompts import DEFAULT_PERSONA, DEFAULT_RUBRIC, FIELD_GUIDANCE

log = logging.getLogger("jobscout.llm")

ApplyMethod = Literal["easy-apply", "email-cv", "company-ats", "gumtree-message", "application-form", "agency", "unknown"]


class Contact(BaseModel):
    kind: Literal["email", "phone", "person", "url"]
    value: str
    label: str


class JobAssessment(BaseModel):
    advert_title: str
    advert_company: str
    advert_location: str
    fit_score: int
    category: str  # narrowed to the configured categories by assessment_model()
    why: str
    cv_angle: str
    seniority_fit: Literal["entry", "junior-ok", "stretch", "too-senior"]
    red_flags: list[str]
    apply_method: ApplyMethod
    apply_steps: list[str]
    contacts: list[Contact]
    is_remote: bool


@lru_cache(maxsize=8)
def assessment_model(cats: tuple[str, ...]) -> type[JobAssessment]:
    """JobAssessment with `category` constrained to the user's categories, so structured output
    can only return one of them."""
    return create_model("JobAssessment", __base__=JobAssessment, category=(Literal[cats], ...))


class ExtractedJob(BaseModel):
    title: str
    company: str
    location: str
    salary: str
    url: str
    summary: str


class ExtractedJobs(BaseModel):
    jobs: list[ExtractedJob]


class BudgetExceeded(RuntimeError):
    pass


# Failures that say nothing about the job being scored - stop and retry later rather than
# marking every queued job as failed (a mistyped model name used to zero the whole backlog).
SYSTEMIC_ERRORS = (
    anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError,
    anthropic.OverloadedError, anthropic.ServiceUnavailableError, anthropic.DeadlineExceededError,
    anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError,
    anthropic.CredentialsError,
)


_client: anthropic.Anthropic | None = None
_budget_lock = threading.Lock()


def available() -> bool:
    return bool(env("ANTHROPIC_API_KEY"))


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"), max_retries=4, timeout=120)
    return _client


def usage_today() -> int:
    return get_kv("llm_usage", {}).get(utc_today(), 0)


def _spend(settings: AppSettings) -> None:
    with _budget_lock:
        today = utc_today()
        usage = get_kv("llm_usage", {})
        used = usage.get(today, 0)
        if used >= settings.llm.daily_limit:
            raise BudgetExceeded(f"daily LLM limit reached ({used}/{settings.llm.daily_limit})")
        set_kv("llm_usage", {today: used + 1})  # only keep today's counter




def scoring_system(settings: AppSettings, profile_text: str) -> str:
    rubric = settings.llm.scoring_rubric.strip() or DEFAULT_RUBRIC
    return f"{rubric}\n\n{FIELD_GUIDANCE}\n\n<candidate_profile>\n{profile_text}\n</candidate_profile>"


def score_job(settings: AppSettings, profile_text: str, job: dict) -> JobAssessment:
    _spend(settings)
    system = scoring_system(settings, profile_text)
    advert = (
        f"<advert source=\"{job['source']}\">\n"
        f"Title: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n"
        f"Salary: {job['salary'] or 'not stated'}\nURL: {job['url']}\n\n{job['description'][:12000]}\n</advert>"
    )
    resp = client().messages.parse(
        model=settings.llm.score_model,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": advert}],
        output_format=assessment_model(tuple(categories(settings))),
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"no assessment returned (stop_reason={resp.stop_reason})")
    return resp.parsed_output


def extract_jobs(settings: AppSettings, page_text: str, page_url: str, hint: str = "") -> list[ExtractedJob]:
    """Pull job listings out of an arbitrary careers/search page (markdown or plain text)."""
    _spend(settings)
    prompt = (
        f"Page URL: {page_url}\n{hint}\n\n"
        "List every individual job vacancy shown on this page. For each, give the title, the hiring company "
        "(the page owner if not stated), location, salary as written (empty if absent), the absolute URL of "
        "that job's own page (resolve relative links against the page URL; use the page URL if there is no "
        "per-job link), and a one or two sentence summary. Ignore navigation, ads, and 'similar jobs' widgets "
        "that are not real listings. Return an empty list if the page has no vacancies.\n\n"
        f"<page>\n{page_text[:60000]}\n</page>"
    )
    resp = client().messages.parse(
        model=settings.llm.score_model,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
        output_format=ExtractedJobs,
    )
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"job extraction cut off at max_tokens for {page_url} - page lists too many jobs")
    if resp.parsed_output is None:
        log.info("no jobs extracted from %s (stop_reason=%s)", page_url, resp.stop_reason)
        return []
    return resp.parsed_output.jobs


def draft_cover_note(settings: AppSettings, profile_text: str, job: dict) -> str:
    _spend(settings)
    resp = client().messages.create(
        model=settings.llm.writer_model,
        max_tokens=4000,
        output_config={"effort": "medium"},
        system=(
            f"You write short, specific application notes for {settings.llm.writer_persona.strip() or DEFAULT_PERSONA}. "
            "Plain British English, no clichés, no invented experience - only use facts from the profile. "
            "Anything in [square brackets] in the profile is an unfilled placeholder, not a fact: never state it "
            "or its examples as true; where it would matter, leave a short [bracketed gap] for the candidate to fill. "
            "Match the format to how this job is applied for: a 120-180 word cover email or message for email/"
            "Gumtree/agency/easy-apply routes, or a 250-350 word supporting statement mapped to the listed "
            "requirements for university or public-sector forms. Output only the note."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"<candidate_profile>\n{profile_text}\n</candidate_profile>\n\n"
                    f"<job apply_method=\"{job['apply_method']}\" source=\"{job['source']}\">\n"
                    f"{job['title']} at {job['company']} ({job['location']})\n\n{job['description'][:12000]}\n</job>"
                ),
            }
        ],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()
