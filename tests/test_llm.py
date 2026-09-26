"""LLM layer with a mocked client: schema handling, score adjustment, budget enforcement."""

from types import SimpleNamespace

import pytest

from app import llm, pipeline
from app.config import get_settings, save_settings
from app.db import Job


def assessment(**kw):
    base = dict(advert_title="Graduate Cryptography Engineer", advert_company="Acme", advert_location="Cardiff",
                fit_score=70, category="Security-Crypto", why="Graduate crypto role.", cv_angle="Built X, improving Y.",
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
    assert j.score == 70 + get_settings().filters.category_weights["Security-Crypto"]
    assert j.contacts == [{"kind": "email", "value": "jobs@acme.co.uk", "label": "HR"}]
    assert fake_client.calls[0]["output_format"] is llm.JobAssessment
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
