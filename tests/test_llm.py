"""LLM layer with a mocked client: schema handling, score adjustment, budget enforcement."""

from types import SimpleNamespace

import pytest

from app import llm, pipeline
from app.config import get_settings, save_settings
from app.db import Job


def assessment(**kw):
    base = dict(advert_title="Graduate Cryptography Engineer", advert_company="Acme", advert_location="Cardiff",
                fit_score=70, category="Security", why="Graduate crypto role.", cv_angle="Built X, improving Y.",
                seniority_fit="entry", red_flags=[], apply_method="company-ats", apply_steps=["Apply on site"],
                contacts=[{"kind": "email", "value": "jobs@acme.co.uk", "label": "HR"}], is_remote=False)
    base.update(kw)
    return llm.JobAssessment.model_validate(base)


class FakeMessages:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(parsed_output=self.parsed, stop_reason="end_turn")


@pytest.fixture
def fake_client(monkeypatch):
    msgs = FakeMessages(assessment())
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(messages=msgs))
    return msgs


def job() -> Job:
    return Job(fingerprint="f", title="Graduate Cryptography Engineer", company="Acme", location="Cardiff",
               url="https://e.com/1", sources=[{"source": "reed", "url": "https://e.com/1"}], lat=51.48, lon=-3.18)


def test_score_applies_category_weight(fake_client):
    j = job()
    pipeline.score_job(j, get_settings(), "profile")
    assert j.score == 70 + get_settings().filters.category_weights["Security"]
    assert j.contacts == [{"kind": "email", "value": "jobs@acme.co.uk", "label": "HR"}]
    schema = fake_client.calls[0]["output_format"]
    assert issubclass(schema, llm.JobAssessment)
    assert schema.model_json_schema()["properties"]["category"]["enum"][-1] == "Not-relevant"
    assert fake_client.calls[0]["model"] == get_settings().llm.score_model


def test_score_is_clamped(fake_client):
    fake_client.parsed = assessment(fit_score=99)
    j = job()
    pipeline.score_job(j, get_settings(), "profile")
    assert j.score == 100


def test_remote_penalty_only_without_location(fake_client):
    fake_client.parsed = assessment(category="IT-Support", is_remote=True)
    j = job()
    j.lat = j.lon = None
    pipeline.score_job(j, get_settings(), "profile")
    assert j.score == 70 - get_settings().search.remote_penalty and j.remote


def test_daily_budget_enforced(fake_client):
    s = get_settings()
    s.llm.daily_limit = 2
    save_settings(s)
    llm.score_job(s, "p", pipeline._job_payload(job()))
    llm.score_job(s, "p", pipeline._job_payload(job()))
    with pytest.raises(llm.BudgetExceeded):
        llm.score_job(s, "p", pipeline._job_payload(job()))


def test_search_results_take_model_title_and_location(fake_client, monkeypatch):
    fake_client.parsed = assessment(advert_title="Graduate Security Engineer", advert_company="IMS Technology",
                                    advert_location="Sydney, Australia")
    monkeypatch.setattr("app.geo.geocode", lambda loc, centres: (-33.87, 151.21))
    j = job()
    j.title, j.company, j.location, j.lat, j.lon = "IMS Technology", "Cyber Security Graduate Scheme", "", None, None
    j.sources = [{"source": "brave", "url": "https://e.com/1"}]
    pipeline.score_job(j, get_settings(), "profile")
    assert (j.title, j.company, j.location) == ("Graduate Security Engineer", "IMS Technology", "Sydney, Australia")
    assert j.filtered_out and "outside search area" in j.filter_reason


def test_board_jobs_keep_their_own_fields(fake_client):
    fake_client.parsed = assessment(advert_title="Something Else", advert_location="Swansea")
    j = job()
    pipeline.score_job(j, get_settings(), "profile")
    assert j.title == "Graduate Cryptography Engineer" and j.location == "Cardiff"


def _queue_one_job():
    from app.db import session

    with session() as s:
        s.add(job())
        s.commit()


def _only_job():
    from sqlmodel import select

    from app.db import session

    with session() as s:
        return s.exec(select(Job)).one()


def test_outage_leaves_jobs_queued(monkeypatch):
    import anthropic
    import httpx2

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    _queue_one_job()

    def boom(*a, **k):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))

    monkeypatch.setattr(llm, "score_job", boom)
    pipeline.score_pending()
    j = _only_job()
    assert j.score is None and j.score_attempts == 0


def test_job_that_keeps_failing_is_marked_after_three_attempts(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    _queue_one_job()
    calls = []

    def bad(*a, **k):
        calls.append(1)
        raise RuntimeError("no assessment returned (stop_reason=refusal)")

    monkeypatch.setattr(llm, "score_job", bad)
    pipeline.score_pending()
    j = _only_job()
    assert len(calls) == 3 and j.score == 0 and "refusal" in j.why


def test_extraction_cut_off_raises(fake_client):
    fake_client.parse = lambda **kw: SimpleNamespace(parsed_output=None, stop_reason="max_tokens")
    with pytest.raises(RuntimeError, match="max_tokens"):
        llm.extract_jobs(get_settings(), "page", "https://e.com/careers")


def test_rubric_and_categories_come_from_settings(fake_client):
    s = get_settings()
    s.llm.scoring_rubric = "Score for a nurse moving into health informatics."
    s.filters.category_weights = {"Informatics": 10, "Clinical": -20}
    pipeline.score_job(job(), s, "profile")
    call = fake_client.calls[0]
    assert call["system"].startswith("Score for a nurse")
    assert call["output_format"].model_json_schema()["properties"]["category"]["enum"] == ["Informatics", "Clinical", "Not-relevant"]
