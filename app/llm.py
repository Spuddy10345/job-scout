"""All Claude calls: job scoring, job extraction from pages, cover-note drafting.

Structured outputs (messages.parse + Pydantic) guarantee schema-valid JSON, so the pipeline
never has to repair model output. A per-day call budget guards against runaway spend.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Literal

import anthropic
from pydantic import BaseModel

from .config import AppSettings, env, get_kv, set_kv

log = logging.getLogger("jobscout.llm")

Category = Literal["SWE", "Security-Crypto", "AI-Data", "Hardware-Embedded", "IT-Support", "Adjacent", "Not-relevant"]
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
    category: Category
    why: str
    cv_angle: str
    seniority_fit: Literal["entry", "junior-ok", "stretch", "too-senior"]
    red_flags: list[str]
    apply_method: ApplyMethod
    apply_steps: list[str]
    contacts: list[Contact]
    is_remote: bool


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


_client: anthropic.Anthropic | None = None
_budget_lock = threading.Lock()


def available() -> bool:
    return bool(env("ANTHROPIC_API_KEY"))


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4, timeout=120)
    return _client


def usage_today() -> int:
    return get_kv("llm_usage", {}).get(date.today().isoformat(), 0)


def _spend(settings: AppSettings) -> None:
    with _budget_lock:
        today = date.today().isoformat()
        usage = get_kv("llm_usage", {})
        used = usage.get(today, 0)
        if used >= settings.llm.daily_limit:
            raise BudgetExceeded(f"daily LLM limit reached ({used}/{settings.llm.daily_limit})")
        set_kv("llm_usage", {today: used + 1})  # only keep today's counter


SCORING_RULES = """You screen job adverts for one specific candidate: a new graduate trying to get a first job in tech.

Text in [square brackets] inside the candidate profile is an unfilled placeholder - ignore it rather than treating its examples as facts.

How to score fit_score (0-100) - be honest and calibrated, most adverts land 20-70:
- 85-100: graduate/junior software, security or cryptography roles that clearly want someone at this level, or anything using post-quantum / applied cryptography.
- 65-84: junior/graduate roles in software, data, AI, embedded/hardware, DevOps, QA/test, or security that the candidate could realistically get.
- 45-64: hands-on computer jobs that are not engineering (IT support, technician, service desk, data entry with automation scope, digital apprenticeships) - these are valuable foot-in-the-door roles where the candidate could automate or improve something and put it on their CV.
- 20-44: technical but a stretch (wants 3+ years, niche stack) or only loosely computer-related.
- 0-19: senior/lead roles, non-technical roles, sales/recruitment, or adverts too vague to act on.

Field guidance:
- advert_title / advert_company / advert_location: the job's real title, hiring employer and work location as the advert states them (search-result titles can be jumbled, e.g. "Company | Job title"). Use the town/city and country if known, "Remote (UK)" for UK-remote, or an empty string if the advert doesn't say. For agencies, the company is the agency unless the client is named.
- category: the best single bucket. Use "Not-relevant" for non-computer jobs.
- why: at most two short sentences on why it does or doesn't fit this candidate.
- cv_angle: one concrete, plausible CV bullet the candidate could earn in this job, in the form "Built/automated X, improving Y by Z" - grounded in what the advert says the team does.
- seniority_fit: judged from years of experience and title.
- red_flags: short items, e.g. "commission only", "unpaid", "requires SC clearance", "5+ years", "vague agency advert". Empty list if none.
- apply_method and apply_steps: how an applicant actually applies for THIS advert given its source site and text (e.g. Indeed apply uses the Indeed profile CV; Gumtree uses the reply form; universities want a supporting statement against the person spec; Civil Service uses Success Profiles behaviour statements; agencies want a CV emailed to the consultant). 2-5 imperative steps.
- contacts: only emails, phone numbers, named recruiters/hiring managers or contact URLs that literally appear in the advert. Never invent any. Empty list if none.
- is_remote: true only if the role is fully remote.
"""


def score_job(settings: AppSettings, profile_text: str, job: dict) -> JobAssessment:
    _spend(settings)
    system = f"{SCORING_RULES}\n<candidate_profile>\n{profile_text}\n</candidate_profile>"
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
        output_format=JobAssessment,
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
    if resp.parsed_output is None:
        return []
    return resp.parsed_output.jobs


def draft_cover_note(settings: AppSettings, profile_text: str, job: dict) -> str:
    _spend(settings)
    resp = client().messages.create(
        model=settings.llm.writer_model,
        max_tokens=4000,
        output_config={"effort": "medium"},
        system=(
            "You write short, specific application notes for a new software engineering graduate. "
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
