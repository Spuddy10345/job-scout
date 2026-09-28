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


USAGE = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=4000, cache_creation_input_tokens=0)


class FakeMessages:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(parsed_output=self.parsed, stop_reason="end_turn", usage=USAGE)


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
    fake_client.parse = lambda **kw: SimpleNamespace(parsed_output=None, stop_reason="max_tokens", usage=USAGE)
    with pytest.raises(RuntimeError, match="max_tokens"):
        llm.extract_jobs(get_settings(), "page", "https://e.com/careers")


def test_rubric_and_categories_come_from_settings(fake_client):
    s = get_settings()
    s.llm.scoring_rubric = "Score for a nurse moving into health informatics."
    s.filters.category_weights = {"Informatics": 10, "Clinical": -20}
    pipeline.score_job(job(), s, "profile")
    call = fake_client.calls[0]
    assert call["system"][0]["text"].startswith("Score for a nurse")
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_format"].model_json_schema()["properties"]["category"]["enum"] == ["Informatics", "Clinical", "Not-relevant"]


def test_usage_is_priced_and_recorded(fake_client):
    s = get_settings()  # haiku: $1 in / $5 out per MTok, cache reads at 10%
    llm.score_job(s, "p", pipeline._job_payload(job()))
    expected = (1000 * 1 + 200 * 5 + 4000 * 1 * 0.1) / 1e6
    assert llm.usage_today() == 1 and abs(llm.cost_today() - expected) < 1e-9
    day = llm.usage_by_day()[next(iter(llm.usage_by_day()))]
    assert (day["in"], day["out"], day["cache_read"]) == (1000, 200, 4000)


def test_dollar_budget_enforced(fake_client):
    s = get_settings()
    s.llm.daily_budget_usd = 0.000001
    save_settings(s)
    llm.score_job(s, "p", pipeline._job_payload(job()))  # first call is allowed, then we're over
    with pytest.raises(llm.BudgetExceeded, match="budget"):
        llm.score_job(s, "p", pipeline._job_payload(job()))


def test_price_matches_longest_prefix():
    s = get_settings()
    assert llm.price(s, "claude-opus-5-5") == (4.0, 20.0)
    assert llm.price(s, "claude-opus-5") == (5.0, 25.0)
    assert llm.price(s, "unknown-model") == (0.0, 0.0)


class FakeBatches:
    def __init__(self):
        self.created = None

    def create(self, requests):
        self.created = requests
        return SimpleNamespace(id="msgbatch_1")

    def retrieve(self, batch_id):
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id):
        good = assessment(fit_score=60).model_dump_json()
        msg = SimpleNamespace(model="claude-haiku-4-5", usage=USAGE, content=[SimpleNamespace(type="text", text=good)])
        yield SimpleNamespace(custom_id="job-7", result=SimpleNamespace(type="succeeded", message=msg))
        yield SimpleNamespace(custom_id="job-8", result=SimpleNamespace(type="expired"))


def test_batch_round_trip(monkeypatch):
    batches = FakeBatches()
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(messages=SimpleNamespace(batches=batches)))
    s = get_settings()
    assert llm.submit_score_batch(s, "profile", {7: pipeline._job_payload(job()), 8: pipeline._job_payload(job())}) == "msgbatch_1"
    params = batches.created[0]["params"]
    assert batches.created[0]["custom_id"] == "job-7"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["output_config"]["format"]["schema"]["additionalProperties"] is False
    results = list(llm.batch_results(s, "msgbatch_1"))
    assert results[0][0] == 7 and results[0][1].fit_score == 60
    assert results[1] == (8, None, "expired")
    assert llm.usage_today() == 2  # both reserved at submission
    assert abs(llm.cost_today() - 0.5 * (1000 + 200 * 5 + 400) / 1e6) < 1e-9  # batch half price


def test_rescore_all_uses_a_batch_and_polling_applies_it(monkeypatch):
    from app.db import session

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    with session() as s:
        for i in range(25):
            j = job()
            j.fingerprint, j.url, j.score = f"f{i}", f"https://e.com/{i}", 40
            s.add(j)
        s.commit()

    class EchoBatches(FakeBatches):
        def results(self, batch_id):
            text = assessment(fit_score=61, category="SWE").model_dump_json()
            msg = SimpleNamespace(model="claude-haiku-4-5", usage=USAGE, content=[SimpleNamespace(type="text", text=text)])
            for r in self.created:
                yield SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(type="succeeded", message=msg))

    batches = EchoBatches()
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(messages=SimpleNamespace(batches=batches)))
    assert pipeline.rescore_all() == 25 and pipeline.batches_pending() == 25
    assert pipeline.score_pending() == 0  # jobs in a batch aren't scored twice
    assert pipeline.poll_batches() == 25
    from sqlmodel import select

    with session() as s:
        jobs = s.exec(select(Job)).all()
    assert {j.score for j in jobs} == {61 + get_settings().filters.category_weights["SWE"]}
    assert all(j.batch_id == "" for j in jobs) and pipeline.batches_pending() == 0
